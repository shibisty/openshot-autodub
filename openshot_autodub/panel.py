"""
 @file
 @brief "Dubbing" dock: speech -> editable sentences -> translation ->
        CosyVoice3 voice clone -> timeline placement -> export.
"""

import functools
import json
import os

from qt_api import (
    Qt, QTimer, QWidget, QLabel, QPushButton, QComboBox, QSpinBox, QCheckBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QProgressBar, QMenu, QInputDialog, QMessageBox,
    QColor, QSizePolicy,
)

from classes.app import get_app
from classes.logger import log
from classes.query import File
from .session import (
    COSYVOICE_LANGUAGES, DubbingSession, Segment, format_time, parse_time,
    STATUS_NEW, STATUS_TRANSLATED, STATUS_DUBBING, STATUS_DUBBED, STATUS_ERROR, STATUS_STALE,
)
from .worker_process import WorkerProcess
from .config import campplus_path, setting
from .comfy_dubber import ComfyDubber, DubJob
from .local_tts import LocalTtsEngine
from . import timeline_placer
from .doctor import Doctor, OK, WARN, MISSING, RUNNING

DUBBING_DOCK_MIN_WIDTH = 620

COL_PLAY, COL_TIME, COL_SPEAKER, COL_TEXT, COL_TRANSLATE, COL_TRANSLATION, \
    COL_DUB, COL_PLAY_DUB, COL_STATUS, COL_DELETE = range(10)

SOURCE_LANGUAGES = [("auto", "Авто")] + [
    ("en", "English"), ("ru", "Русский"), ("uk", "Українська"), ("es", "Español"), ("de", "Deutsch"),
    ("fr", "Français"), ("it", "Italiano"), ("pt", "Português"), ("pl", "Polski"), ("tr", "Türkçe"),
    ("ja", "日本語"), ("ko", "한국어"), ("zh", "中文"),
]

STATUS_LABELS = {
    STATUS_NEW: ("", None),
    STATUS_TRANSLATED: ("переведено", "#6fa8dc"),
    STATUS_DUBBING: ("озвучка…", "#f1c232"),
    STATUS_DUBBED: ("готово", "#6aa84f"),
    STATUS_ERROR: ("ошибка", "#e06666"),
    STATUS_STALE: ("устарело", "#e69138"),
}


def _(text):
    try:
        return get_app()._tr(text)
    except Exception:
        return text


class DubbingDockContent(QWidget):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.session = None
        self.worker = None
        self.dubber = None
        self._stop_frame = None
        self._file_mode = False
        self._updating = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_now)
        self._build_ui()
        self.refresh_videos()
        preview = getattr(window, "preview_thread", None)
        if preview is not None and hasattr(preview, "position_changed"):
            preview.position_changed.connect(self._on_position_changed)
        get_app().updates.add_listener(self)
        self.doctor = Doctor.instance()
        self.doctor.updated.connect(self._update_env_label)
        self.doctor.finished.connect(self._update_env_label)

    # ======================================================= environment ==
    def on_shown(self):
        self.refresh_videos()
        if not self.doctor.has_run:
            self.doctor.run_all(self._languages())
        self._update_env_label()

    def _languages(self):
        return (self.src_combo.currentData() or "auto", self.dst_combo.currentData() or "ru")

    def _update_env_label(self):
        status, text = self.doctor.summary()
        color = {OK: "#6aa84f", WARN: "#e69138", MISSING: "#e06666", RUNNING: "#999999"}.get(status, "#999999")
        self.env_label.setText(text)
        self.env_label.setStyleSheet("color:%s;" % color)

    def open_setup(self, group=None, reason=""):
        from .setup_dialog import SetupDialog
        dlg = SetupDialog(self, languages=self._languages(), focus_group=group, reason=reason)
        dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()
        self._update_env_label()

    def _require(self, group):
        """True when the environment for `group` (asr/translate/tts) is ready; otherwise explain/offer setup."""
        names = {"asr": _("распознавания"), "translate": _("перевода"), "tts": _("озвучки")}
        if not self.doctor.has_run:
            self.doctor.run_all(self._languages())
        if self.doctor.running():
            self._status(_("Идёт проверка окружения, повторите через пару секунд"))
            return False
        blocking = self.doctor.blocking(group)
        if blocking:
            self.open_setup(group, _("Для %s не хватает: %s") % (
                names.get(group, group), ", ".join(c.title for c in blocking)))
            return not self.doctor.blocking(group) and not self.doctor.running()
        return True

    # ================================================================ UI ==
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # --- environment status
        row = QHBoxLayout()
        self.env_label = QLabel(_("Окружение не проверено"))
        self.env_label.setWordWrap(True)
        row.addWidget(self.env_label, 1)
        btn_setup = QPushButton(_("Настройка и проверка…"))
        btn_setup.setToolTip(_("Проверить Python, пакеты и модели; скачать недостающее"))
        btn_setup.clicked.connect(lambda: self.open_setup())
        row.addWidget(btn_setup)
        root.addLayout(row)

        # --- source video
        row = QHBoxLayout()
        row.addWidget(QLabel(_("Видео:")))
        self.video_combo = QComboBox()
        self.video_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.video_combo.currentIndexChanged.connect(self._video_changed)
        row.addWidget(self.video_combo, 1)
        btn_import = QPushButton(_("Импорт…"))
        btn_import.setToolTip(_("Импортировать видео в проект"))
        btn_import.clicked.connect(self._import_video)
        row.addWidget(btn_import)
        btn_reload = QPushButton("⟳")
        btn_reload.setToolTip(_("Обновить список файлов проекта"))
        btn_reload.setMaximumWidth(30)
        btn_reload.clicked.connect(self.refresh_videos)
        row.addWidget(btn_reload)
        root.addLayout(row)

        # --- languages + transcribe
        row = QHBoxLayout()
        row.addWidget(QLabel(_("Язык оригинала:")))
        self.src_combo = QComboBox()
        for code, name in SOURCE_LANGUAGES:
            self.src_combo.addItem(name, code)
        self.src_combo.currentIndexChanged.connect(self._lang_changed)
        row.addWidget(self.src_combo)
        row.addWidget(QLabel(_("Говорящих:")))
        self.speakers_spin = QSpinBox()
        self.speakers_spin.setRange(0, 12)
        self.speakers_spin.setSpecialValueText(_("авто"))
        self.speakers_spin.setToolTip(_("0 = определить автоматически"))
        row.addWidget(self.speakers_spin)
        self.btn_transcribe = QPushButton(_("Распознать"))
        self.btn_transcribe.setToolTip(_("Whisper просматривает всё видео: предложения, говорящие, таймкоды"))
        self.btn_transcribe.clicked.connect(self.transcribe)
        row.addWidget(self.btn_transcribe)
        row.addStretch(1)
        row.addWidget(QLabel(_("Перевод на:")))
        self.dst_combo = QComboBox()
        for code, name in COSYVOICE_LANGUAGES:
            self.dst_combo.addItem(name, code)
        self.dst_combo.currentIndexChanged.connect(self._lang_changed)
        row.addWidget(self.dst_combo)
        root.addLayout(row)

        # --- progress
        row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        row.addWidget(self.progress, 1)
        self.status_label = QLabel("")
        row.addWidget(self.status_label, 2)
        self.btn_cancel = QPushButton(_("Стоп"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_jobs)
        row.addWidget(self.btn_cancel)
        root.addLayout(row)

        # --- sentence table
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels([
            "", _("Время"), _("Спикер"), _("Оригинал"), "", _("Перевод"), "", "", _("Статус"), ""])
        header = self.table.horizontalHeader()
        for col in (COL_PLAY, COL_TRANSLATE, COL_DUB, COL_PLAY_DUB, COL_DELETE):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, 30)
        header.setSectionResizeMode(COL_TIME, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_SPEAKER, QHeaderView.Interactive)
        self.table.setColumnWidth(COL_SPEAKER, 95)
        header.setSectionResizeMode(COL_TEXT, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_TRANSLATION, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                   | QAbstractItemView.SelectedClicked)
        self.table.itemChanged.connect(self._item_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        root.addWidget(self.table, 1)

        # --- batch actions
        row = QHBoxLayout()
        btn = QPushButton(_("+ Строка у плейхеда"))
        btn.clicked.connect(self.add_row_at_playhead)
        row.addWidget(btn)
        row.addStretch(1)
        self.btn_translate_all = QPushButton(_("Перевести всё"))
        self.btn_translate_all.setToolTip(_("Переводит строки без перевода (выделенные – если есть выделение)"))
        self.btn_translate_all.clicked.connect(self.translate_pending)
        row.addWidget(self.btn_translate_all)
        self.btn_dub_all = QPushButton(_("Озвучить всё"))
        self.btn_dub_all.setToolTip(_("CosyVoice3: озвучивает переведённые строки без готового дубляжа"))
        self.btn_dub_all.clicked.connect(self.dub_pending)
        row.addWidget(self.btn_dub_all)
        root.addLayout(row)

        # --- mix settings
        box = QGroupBox(_("Сведение (перед переносом на таймлайн)"))
        grid = QGridLayout(box)
        self.chk_overlay = QCheckBox(_("Дубляж поверх оригинальной дорожки"))
        self.chk_overlay.setToolTip(_("Выключено – оригинальный звук видео заглушается полностью"))
        self.spin_orig = QSpinBox()
        self.spin_orig.setRange(0, 100)
        self.spin_orig.setSuffix(" %")
        self.spin_dub = QSpinBox()
        self.spin_dub.setRange(0, 200)
        self.spin_dub.setSuffix(" %")
        self.chk_duck = QCheckBox(_("Приглушать оригинал только во время реплик"))
        self.chk_fit = QCheckBox(_("Ускорять реплику, если не влезает в своё время"))
        self.chk_tracks = QCheckBox(_("Отдельная дорожка на каждого спикера"))
        self.diff_label = QLabel("")
        grid.addWidget(self.chk_overlay, 0, 0, 1, 2)
        grid.addWidget(QLabel(_("Громкость оригинала:")), 1, 0)
        grid.addWidget(self.spin_orig, 1, 1)
        grid.addWidget(QLabel(_("Громкость дубляжа:")), 1, 2)
        grid.addWidget(self.spin_dub, 1, 3)
        grid.addWidget(self.diff_label, 1, 4)
        grid.addWidget(self.chk_duck, 2, 0, 1, 3)
        grid.addWidget(self.chk_fit, 3, 0, 1, 3)
        grid.addWidget(self.chk_tracks, 4, 0, 1, 3)
        for w in (self.chk_overlay, self.chk_duck, self.chk_fit, self.chk_tracks):
            w.toggled.connect(self._mix_changed)
        for w in (self.spin_orig, self.spin_dub):
            w.valueChanged.connect(self._mix_changed)
        root.addWidget(box)

        row = QHBoxLayout()
        self.btn_place = QPushButton(_("Разместить дубляж на таймлайне"))
        self.btn_place.clicked.connect(self.place_on_timeline)
        row.addWidget(self.btn_place, 1)
        btn = QPushButton(_("Экспорт MP4…"))
        btn.clicked.connect(lambda: self.window.actionExportVideo_trigger())
        row.addWidget(btn)
        root.addLayout(row)
        self._set_enabled(False)

    def _set_enabled(self, has_session):
        for w in (self.btn_transcribe, self.btn_translate_all, self.btn_dub_all, self.btn_place, self.table):
            w.setEnabled(bool(has_session))

    # ============================================================ videos ==
    def changed(self, action):
        """UpdateManager listener: refresh video list when files change."""
        try:
            if action.type == "load" or (action.key and action.key[0] == "files"):
                QTimer.singleShot(0, self.refresh_videos)
        except Exception:
            pass

    def refresh_videos(self):
        current = self.video_combo.currentData()
        self.video_combo.blockSignals(True)
        self.video_combo.clear()
        self.video_combo.addItem(_("— выберите видео —"), None)
        for f in File.filter():
            data = f.data
            if not data.get("has_audio", False):
                continue
            path = f.absolute_path()
            if self.session and self.session.dir and os.path.abspath(path).startswith(self.session.dir):
                continue  # our own dub files
            if not data.get("has_video", False) and data.get("media_type") != "video":
                continue
            self.video_combo.addItem(data.get("name") or os.path.basename(path), f.id)
        idx = self.video_combo.findData(current) if current else -1
        self.video_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.video_combo.blockSignals(False)
        if idx < 0 and self.video_combo.count() == 2 and not self.session:
            self.video_combo.setCurrentIndex(1)

    def _import_video(self):
        trigger = getattr(self.window, "actionImportFiles_trigger", None)
        if callable(trigger):
            trigger()
        QTimer.singleShot(300, self.refresh_videos)

    def _video_changed(self, _index):
        file_id = self.video_combo.currentData()
        self._save_now()
        if not file_id:
            self.session = None
            self._set_enabled(False)
            self.table.setRowCount(0)
            return
        f = File.get(id=file_id)
        if not f:
            return
        self.session = DubbingSession.load_or_create(f.absolute_path(), file_id)
        self._load_session_into_ui()
        self._set_enabled(True)

    def _load_session_into_ui(self):
        s = self.session
        self._updating = True
        try:
            self._select_combo(self.src_combo, s.source_language)
            self._select_combo(self.dst_combo, s.target_language)
            self.chk_overlay.setChecked(bool(s.mix.get("overlay", True)))
            self.spin_orig.setValue(int(s.mix.get("original_volume", 25)))
            self.spin_dub.setValue(int(s.mix.get("dub_volume", 100)))
            self.chk_duck.setChecked(bool(s.mix.get("duck_only_speech", True)))
            self.chk_fit.setChecked(bool(s.mix.get("fit_speed", True)))
            self.chk_tracks.setChecked(bool(s.mix.get("track_per_speaker", True)))
        finally:
            self._updating = False
        self._update_mix_ui()
        self.rebuild_table()
        n = len(s.segments)
        self._status(_("Сессия: %s – %d строк") % (s.dir, n) if n else _("Нажмите «Распознать»"))

    @staticmethod
    def _select_combo(combo, value):
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _lang_changed(self, *_args):
        if self._updating or not self.session:
            return
        self.session.source_language = self.src_combo.currentData() or "auto"
        new_target = self.dst_combo.currentData() or "ru"
        if new_target != self.session.target_language:
            self.session.target_language = new_target
            for seg in self.session.segments:
                if seg.get("translation"):
                    seg["status"] = STATUS_STALE if seg.get("dub_path") else STATUS_NEW
            self.rebuild_table()
        self._schedule_save()

    def _mix_changed(self, *_args):
        self._update_mix_ui()
        if self._updating or not self.session:
            return
        self.session.mix.update({
            "overlay": self.chk_overlay.isChecked(),
            "original_volume": self.spin_orig.value(),
            "dub_volume": self.spin_dub.value(),
            "duck_only_speech": self.chk_duck.isChecked(),
            "fit_speed": self.chk_fit.isChecked(),
            "track_per_speaker": self.chk_tracks.isChecked(),
        })
        self._schedule_save()

    def _update_mix_ui(self):
        overlay = self.chk_overlay.isChecked()
        self.spin_orig.setEnabled(overlay)
        self.chk_duck.setEnabled(overlay)
        if overlay and self.spin_orig.value() > 0:
            import math
            db = 20.0 * math.log10(max(1e-6, self.spin_orig.value() / max(1, self.spin_dub.value())))
            self.diff_label.setText(_("оригинал %+.1f dB к дубляжу") % db)
        else:
            self.diff_label.setText(_("оригинал выключен"))

    # ============================================================= table ==
    def rebuild_table(self):
        self._updating = True
        self.table.setRowCount(0)
        if not self.session:
            self._updating = False
            return
        self.table.setRowCount(len(self.session.segments))
        for row, seg in enumerate(self.session.segments):
            self._fill_row(row, seg)
        self.table.resizeRowsToContents()
        self._updating = False

    def _btn(self, text, tip, slot, seg_id):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setFlat(True)
        b.setMaximumWidth(28)
        b.clicked.connect(functools.partial(slot, seg_id))
        return b

    def _fill_row(self, row, seg):
        sid = seg["id"]
        time_item = QTableWidgetItem("%s – %s" % (format_time(seg["start"]), format_time(seg["end"])))
        time_item.setData(Qt.UserRole, sid)
        self.table.setItem(row, COL_TIME, time_item)
        self.table.setItem(row, COL_SPEAKER, QTableWidgetItem(seg.get("speaker", "")))
        self.table.setItem(row, COL_TEXT, QTableWidgetItem(seg.get("text", "")))
        self.table.setItem(row, COL_TRANSLATION, QTableWidgetItem(seg.get("translation", "")))
        label, color = STATUS_LABELS.get(seg.get("status", ""), ("", None))
        if seg.get("status") == STATUS_DUBBED and seg.get("dub_duration"):
            label = "%.1fs%s" % (seg["dub_duration"],
                                 " ×%.2f" % seg["dub_speed"] if float(seg.get("dub_speed", 1)) > 1.01 else "")
        st = QTableWidgetItem(label)
        st.setFlags(st.flags() & ~Qt.ItemIsEditable)
        if color:
            st.setForeground(QColor(color))
        if seg.get("error"):
            st.setToolTip(seg["error"])
        self.table.setItem(row, COL_STATUS, st)
        for col in (COL_PLAY, COL_TRANSLATE, COL_DUB, COL_PLAY_DUB, COL_DELETE):
            item = QTableWidgetItem("")
            item.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, col, item)
        self.table.setCellWidget(row, COL_PLAY, self._btn("▶", _("Проиграть фрагмент в видео"), self.play_original, sid))
        self.table.setCellWidget(row, COL_TRANSLATE, self._btn("⇄", _("Перевести строку"), self.translate_one, sid))
        self.table.setCellWidget(row, COL_DUB, self._btn("♪", _("Озвучить голосом спикера (CosyVoice3)"), self.dub_one, sid))
        play_dub = self._btn("▷", _("Прослушать дубляж"), self.play_dub, sid)
        play_dub.setEnabled(bool(seg.get("dub_path")) and os.path.isfile(seg.get("dub_path", "")))
        self.table.setCellWidget(row, COL_PLAY_DUB, play_dub)
        self.table.setCellWidget(row, COL_DELETE, self._btn("✕", _("Удалить строку (шум)"), self.delete_row, sid))

    def _row_of(self, seg_id):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_TIME)
            if item and item.data(Qt.UserRole) == seg_id:
                return row
        return -1

    def refresh_row(self, seg_id):
        row = self._row_of(seg_id)
        seg = self.session.segment(seg_id) if self.session else None
        if row < 0 or not seg:
            return
        self._updating = True
        self._fill_row(row, seg)
        self.table.resizeRowToContents(row)
        self._updating = False

    def _item_changed(self, item):
        if self._updating or not self.session:
            return
        sid_item = self.table.item(item.row(), COL_TIME)
        seg = self.session.segment(sid_item.data(Qt.UserRole)) if sid_item else None
        if not seg:
            return
        col = item.column()
        text = item.text().strip()
        if col == COL_TIME:
            try:
                a, b = [parse_time(p) for p in text.replace("—", "–").replace(" - ", "–").split("–")]
                if b <= a:
                    raise ValueError
                seg["start"], seg["end"] = a, b
                self._mark_stale(seg)
                self.session.sort()
                QTimer.singleShot(0, self.rebuild_table)
            except Exception:
                QMessageBox.warning(self, _("Время"), _("Формат: 0:01.250 – 0:03.900"))
                QTimer.singleShot(0, functools.partial(self.refresh_row, seg["id"]))
                return
        elif col == COL_SPEAKER:
            seg["speaker"] = text or "SPEAKER_A"
            self._mark_stale(seg)
        elif col == COL_TEXT:
            if text != seg.get("text"):
                seg["text"] = text
                if seg.get("translation"):
                    seg["status"] = STATUS_STALE if seg.get("dub_path") else STATUS_NEW
                QTimer.singleShot(0, functools.partial(self.refresh_row, seg["id"]))
        elif col == COL_TRANSLATION:
            if text != seg.get("translation"):
                seg["translation"] = text
                self._mark_stale(seg, translated=True)
                QTimer.singleShot(0, functools.partial(self.refresh_row, seg["id"]))
        self._schedule_save()

    @staticmethod
    def _mark_stale(seg, translated=False):
        if seg.get("dub_path"):
            seg["status"] = STATUS_STALE
        elif translated and seg.get("translation"):
            seg["status"] = STATUS_TRANSLATED

    def _selected_ids(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        ids = []
        for row in rows:
            item = self.table.item(row, COL_TIME)
            if item:
                ids.append(item.data(Qt.UserRole))
        return ids

    def _context_menu(self, pos):
        if not self.session:
            return
        row = self.table.rowAt(pos.y())
        item = self.table.item(row, COL_TIME) if row >= 0 else None
        seg = self.session.segment(item.data(Qt.UserRole)) if item else None
        menu = QMenu(self)
        if seg:
            menu.addAction(_("Начало = плейхед"), functools.partial(self._set_edge_from_playhead, seg, "start"))
            menu.addAction(_("Конец = плейхед"), functools.partial(self._set_edge_from_playhead, seg, "end"))
            menu.addSeparator()
            menu.addAction(_("Переименовать спикера «%s» везде…") % seg["speaker"],
                           functools.partial(self._rename_speaker, seg["speaker"]))
            menu.addAction(_("Использовать эту строку как образец голоса «%s»") % seg["speaker"],
                           functools.partial(self._set_speaker_ref, seg))
            if seg["speaker"] in self.session.speaker_refs:
                menu.addAction(_("Сбросить образец голоса «%s»") % seg["speaker"],
                               functools.partial(self._clear_speaker_ref, seg["speaker"]))
            menu.addSeparator()
            menu.addAction(_("Объединить со следующей"), functools.partial(self._merge_next, seg))
            menu.addAction(_("Вставить пустую строку после"), functools.partial(self._insert_after, seg))
        ids = self._selected_ids()
        if ids:
            menu.addSeparator()
            menu.addAction(_("Перевести выделенные (%d)") % len(ids), lambda: self.translate_ids(ids))
            menu.addAction(_("Озвучить выделенные (%d)") % len(ids), lambda: self.dub_ids(ids))
            menu.addAction(_("Удалить выделенные (%d)") % len(ids), lambda: self._delete_ids(ids))
        menu.exec_(self.table.viewport().mapToGlobal(pos))

    def _source_time_at_playhead(self):
        try:
            frame = int(self.window.preview_thread.player.Position())
        except Exception:
            frame = 1
        fps = timeline_placer.project_fps()
        t = (frame - 1) / fps
        clip = timeline_placer.source_clip(self.session)
        if clip:
            t = t - float(clip.data.get("position", 0.0)) + float(clip.data.get("start", 0.0))
        return max(0.0, t)

    def _set_edge_from_playhead(self, seg, edge):
        t = self._source_time_at_playhead()
        if edge == "start" and t < float(seg["end"]):
            seg["start"] = t
        elif edge == "end" and t > float(seg["start"]):
            seg["end"] = t
        else:
            return
        self._mark_stale(seg)
        self.session.sort()
        self.rebuild_table()
        self._schedule_save()

    def _rename_speaker(self, old):
        new, ok = QInputDialog.getText(self, _("Спикер"), _("Новое имя для «%s»:") % old, text=old)
        if ok and new.strip() and new.strip() != old:
            self.session.rename_speaker(old, new.strip())
            self.rebuild_table()
            self._schedule_save()

    def _set_speaker_ref(self, seg):
        try:
            from .session import write_wav_slices
            out = os.path.join(self.session.ref_dir(), "speaker_%s.wav" % seg["id"])
            write_wav_slices(self.session.audio16k, [(seg["start"], seg["end"])], out)
            self.session.speaker_refs[seg["speaker"]] = {"path": out, "text": seg.get("text", "")}
            self._status(_("Образец голоса «%s» = строка %s") % (seg["speaker"], format_time(seg["start"])))
            self._schedule_save()
        except Exception as exc:
            QMessageBox.warning(self, _("Образец голоса"), str(exc))

    def _clear_speaker_ref(self, speaker):
        self.session.speaker_refs.pop(speaker, None)
        self._schedule_save()

    def _merge_next(self, seg):
        idx = self.session.index_of(seg["id"])
        if idx < 0 or idx + 1 >= len(self.session.segments):
            return
        nxt = self.session.segments[idx + 1]
        seg["end"] = max(float(seg["end"]), float(nxt["end"]))
        seg["text"] = (seg.get("text", "") + " " + nxt.get("text", "")).strip()
        if seg.get("translation") or nxt.get("translation"):
            seg["translation"] = (seg.get("translation", "") + " " + nxt.get("translation", "")).strip()
        self._mark_stale(seg, translated=bool(seg.get("translation")))
        self.session.remove(nxt["id"])
        self.rebuild_table()
        self._schedule_save()

    def _insert_after(self, seg):
        start = float(seg["end"]) + 0.05
        new = Segment.new(start, start + 1.5, "", seg["speaker"])
        self.session.segments.insert(self.session.index_of(seg["id"]) + 1, new)
        self.rebuild_table()
        self._schedule_save()

    def add_row_at_playhead(self):
        if not self.session:
            return
        t = self._source_time_at_playhead()
        speaker = self.session.speakers()[0] if self.session.segments else "SPEAKER_A"
        self.session.segments.append(Segment.new(t, t + 2.0, "", speaker))
        self.session.sort()
        self.rebuild_table()
        self._schedule_save()

    def delete_row(self, seg_id):
        self._delete_ids([seg_id])

    def _delete_ids(self, ids):
        for sid in ids:
            self.session.remove(sid)
        self.rebuild_table()
        self._schedule_save()

    # ========================================================== playback ==
    def play_original(self, seg_id):
        seg = self.session.segment(seg_id) if self.session else None
        if not seg:
            return
        clip = timeline_placer.ensure_source_clip(self.window, self.session)
        if not clip:
            self._status(_("Видео нет на таймлайне"))
            return
        fps = timeline_placer.project_fps()
        start = timeline_placer.timeline_seconds(clip.data, seg["start"])
        end = timeline_placer.timeline_seconds(clip.data, seg["end"])
        start_frame = max(1, int(round(start * fps)) + 1)
        stop_frame = int(round(end * fps)) + 1

        def go():
            self._stop_frame = stop_frame
            self.window.SeekSignal.emit(start_frame, True)
            self.window.PlaySignal.emit()
            QTimer.singleShot(int((end - start + 1.5) * 1000), self._safety_pause)

        if self._file_mode:
            self._file_mode = False
            self.window.LoadFileSignal.emit("")
            QTimer.singleShot(200, go)
        else:
            go()
        self._select_row(seg_id)

    def play_dub(self, seg_id):
        seg = self.session.segment(seg_id) if self.session else None
        if not seg or not seg.get("dub_path") or not os.path.isfile(seg["dub_path"]):
            return
        fps = timeline_placer.project_fps()
        self._file_mode = True
        self.window.LoadFileSignal.emit(seg["dub_path"])
        duration = float(seg.get("dub_duration") or seg.duration)

        def go():
            self._stop_frame = int(round(duration * fps)) + 1
            self.window.SeekSignal.emit(1, True)
            self.window.PlaySignal.emit()
            QTimer.singleShot(int((duration + 1.5) * 1000), self._safety_pause)

        QTimer.singleShot(200, go)
        self._select_row(seg_id)

    def _select_row(self, seg_id):
        row = self._row_of(seg_id)
        if row >= 0:
            self.table.selectRow(row)

    def _on_position_changed(self, frame):
        if self._stop_frame is not None and int(frame) >= self._stop_frame:
            self._stop_frame = None
            self.window.PauseSignal.emit()

    def _safety_pause(self):
        if self._stop_frame is not None:
            self._stop_frame = None
            self.window.PauseSignal.emit()

    # ======================================================= transcribe ==
    def _busy(self):
        return (self.worker is not None and self.worker.is_running()) or \
               (self.dubber is not None and self.dubber.isRunning())

    def _engine_name(self):
        return str(setting("tts_engine", "local") or "local")

    def transcribe(self):
        if not self.session or self._busy():
            return
        if not self._require("asr"):
            return
        if self.session.segments:
            answer = QMessageBox.question(
                self, _("Распознать заново"),
                _("Текущие %d строк (и правки) будут заменены. Продолжить?") % len(self.session.segments))
            if answer != QMessageBox.Yes:
                return
        os.makedirs(self.session.dir, exist_ok=True)
        args = [
            "transcribe",
            "--video", self.session.video_path,
            "--workdir", self.session.dir,
            "--model", str(setting("whisper_model", "medium")),
            "--device", str(setting("whisper_device", "cuda")),
            "--compute", str(setting("whisper_compute", "int8")),
            "--language", self.src_combo.currentData() or "auto",
            "--speakers", str(self.speakers_spin.value()),
        ]
        cp = campplus_path()
        if cp:
            args += ["--campplus", cp]
        self._run_worker(args, self._on_transcribed)

    def _on_transcribed(self, result):
        self.session.apply_transcript(result)
        self.session.save()
        self.rebuild_table()
        self._status(_("Распознано %d предложений, язык: %s, спикеров: %d (%s)") % (
            len(self.session.segments), self.session.detected_language,
            len(self.session.speakers()), self.session.diarization))

    # ======================================================== translate ==
    def translate_one(self, seg_id):
        self.translate_ids([seg_id])

    def translate_pending(self):
        if not self.session:
            return
        ids = self._selected_ids() if len(self._selected_ids()) > 1 else [
            s["id"] for s in self.session.segments if not s.get("translation") and s.get("text")]
        if not ids:
            self._status(_("Нечего переводить – все строки уже переведены"))
            return
        self.translate_ids(ids)

    def translate_ids(self, ids):
        if not self.session or self._busy():
            return
        if not self._require("translate"):
            return
        items = [{"id": s["id"], "text": s.get("text", "")} for s in self.session.segments if s["id"] in ids]
        if not items:
            return
        req = {
            "source": self.session.effective_source_language(),
            "target": self.session.target_language,
            "items": items,
        }
        os.makedirs(self.session.dir, exist_ok=True)
        path = os.path.join(self.session.dir, "translate_request.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(req, fh, ensure_ascii=False)
        self._run_worker(["translate", "--request", path], self._on_translated)

    def _on_translated(self, result):
        for it in result.get("items") or []:
            seg = self.session.segment(it["id"])
            if not seg:
                continue
            seg["translation"] = it.get("text", "")
            seg["status"] = STATUS_STALE if seg.get("dub_path") else STATUS_TRANSLATED
            self.refresh_row(seg["id"])
        self.session.save()
        self._status(_("Переведено строк: %d") % len(result.get("items") or []))

    # ============================================================ worker ==
    def _run_worker(self, args, on_result):
        self.worker = WorkerProcess(args, self)
        self.worker.progress.connect(self._on_progress)
        self.worker.result.connect(on_result)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._job_finished)
        self.btn_cancel.setEnabled(True)
        self.progress.setValue(0)
        self._status(_("Запуск…"))
        self.worker.start()

    def _on_progress(self, value, message):
        self.progress.setValue(int(value * 1000))
        if message:
            self._status(message)

    def _on_failed(self, message):
        self._status(_("Ошибка"))
        QMessageBox.warning(self, _("Дубляж"), message)

    def _job_finished(self):
        self.btn_cancel.setEnabled(self._busy())
        self.progress.setValue(1000)

    def cancel_jobs(self):
        if self.worker and self.worker.is_running():
            self.worker.kill()
        if self.dubber and self.dubber.isRunning():
            self.dubber.cancel()
        self._status(_("Остановлено"))

    # ============================================================== dub ==
    def dub_one(self, seg_id):
        self.dub_ids([seg_id])

    def dub_pending(self):
        if not self.session:
            return
        sel = self._selected_ids()
        if len(sel) > 1:
            ids = sel
        else:
            ids = [s["id"] for s in self.session.segments
                   if s.get("translation") and s.get("status") in (STATUS_TRANSLATED, STATUS_STALE, STATUS_ERROR)]
        if not ids:
            self._status(_("Нет переведённых строк без дубляжа"))
            return
        self.dub_ids(ids)

    def dub_ids(self, ids):
        if not self.session or self._busy():
            if self._busy():
                self._status(_("Дождитесь окончания текущей задачи"))
            return
        if not self._require("tts"):
            return
        jobs = []
        for sid in ids:
            seg = self.session.segment(sid)
            if not seg:
                continue
            text = (seg.get("translation") or "").strip()
            if not text:
                seg["status"], seg["error"] = STATUS_ERROR, _("Нет перевода")
                self.refresh_row(sid)
                continue
            try:
                ref, ref_text = self.session.reference_for(seg)
            except Exception as exc:
                seg["status"], seg["error"] = STATUS_ERROR, str(exc)
                self.refresh_row(sid)
                continue
            jobs.append(DubJob(
                seg_id=sid, text=text, ref_path=ref, ref_text=ref_text,
                ref_lang=self.session.effective_source_language(), language=self.session.target_language,
                slot=self.session.slot_for(seg), fit_speed=self.session.mix.get("fit_speed", True),
                out_dir=self.session.dub_dir()))
            seg["status"], seg["error"] = STATUS_DUBBING, ""
            self.refresh_row(sid)
        if not jobs:
            return
        self.btn_cancel.setEnabled(True)
        self.progress.setValue(0)
        if self._engine_name() == "comfyui":
            self.dubber = ComfyDubber(jobs, self)
            self.dubber.segment_done.connect(self._dub_done)
            self.dubber.segment_failed.connect(self._dub_failed)
            self.dubber.queue_progress.connect(self._dub_progress)
            self.dubber.finished.connect(self._dub_all_finished)
            self._status(_("Озвучка 0/%d через ComfyUI…") % len(jobs))
            self.dubber.start()
        else:
            self.dubber = self._local_engine()
            self._status(_("Озвучка 0/%d (CosyVoice локально)…") % len(jobs))
            self.dubber.start_jobs(jobs)

    def _local_engine(self):
        engine = LocalTtsEngine.instance()
        if not getattr(self, "_local_connected", False):
            engine.segment_done.connect(self._dub_done)
            engine.segment_failed.connect(self._dub_failed)
            engine.queue_progress.connect(self._dub_progress)
            engine.finished.connect(self._dub_all_finished)
            engine.status.connect(self._status)
            self._local_connected = True
        return engine

    def _dub_done(self, seg_id, path, duration, speed):
        seg = self.session.segment(seg_id)
        if not seg:
            return
        seg.update({"dub_path": path, "dub_duration": duration, "dub_speed": speed,
                    "dub_text": seg.get("translation", ""), "status": STATUS_DUBBED, "error": ""})
        self.refresh_row(seg_id)
        self._schedule_save()

    def _dub_failed(self, seg_id, message):
        seg = self.session.segment(seg_id)
        if seg:
            seg["status"], seg["error"] = STATUS_ERROR, message
            self.refresh_row(seg_id)
        self._status(message.splitlines()[0][:160])
        self._schedule_save()

    def _dub_progress(self, done, total):
        self.progress.setValue(int(1000 * done / max(1, total)))
        self._status(_("Озвучка %d/%d") % (done, total))

    def _dub_all_finished(self):
        self.btn_cancel.setEnabled(self._busy())
        for seg in self.session.segments if self.session else []:
            if seg.get("status") == STATUS_DUBBING:
                seg["status"] = STATUS_STALE if seg.get("dub_path") else STATUS_TRANSLATED
                self.refresh_row(seg["id"])
        self._save_now()

    # ============================================================ place ==
    def place_on_timeline(self):
        if not self.session:
            return
        stale = [s for s in self.session.segments if s.get("status") in (STATUS_STALE, STATUS_TRANSLATED)]
        if stale:
            answer = QMessageBox.question(
                self, _("Разместить"),
                _("%d строк без актуального дубляжа будут пропущены (или с устаревшим). Продолжить?") % len(stale))
            if answer != QMessageBox.Yes:
                return
        self._mix_changed()
        try:
            self.window.WaitCursorSignal.emit(True)
            placed, skipped = timeline_placer.place_on_timeline(self.window, self.session)
        except Exception as exc:
            log.error("Dubbing placement failed", exc_info=True)
            QMessageBox.warning(self, _("Разместить"), str(exc))
            return
        finally:
            self.window.WaitCursorSignal.emit(False)
        self.session.save()
        self.refresh_videos()
        self._status(_("На таймлайне: %d реплик (пропущено %d). Проверьте и экспортируйте.") % (placed, skipped))

    # ============================================================= misc ==
    def _status(self, text):
        self.status_label.setText(text)
        self.status_label.setToolTip(text)

    def _schedule_save(self):
        self._save_timer.start()

    def _save_now(self):
        if self.session:
            try:
                self.session.save()
            except Exception:
                log.warning("Dubbing: save failed", exc_info=True)
