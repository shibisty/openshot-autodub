"""
"Настройка и проверка" dialog: paths/models form, environment checks with
one-click fixes (create venv, pip install, download models/code) and a live log.
"""

import functools
import os

from qt_api import (
    Qt, QThread, QTimer, pyqtSignal, QObject, QDialog, QWidget, QLabel, QPushButton, QComboBox, QLineEdit,
    QCheckBox, QSpinBox, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QPlainTextEdit, QMessageBox, QFileDialog, QColor, QMenu,
)

from classes.logger import log

from .config import COSYVOICE_MODELS, WHISPER_MODELS, config
from .doctor import Doctor, OK, WARN, MISSING, UNKNOWN
from .worker_process import WorkerProcess

STATUS_VIEW = {
    OK: ("✔", "#6aa84f"),
    WARN: ("!", "#e69138"),
    MISSING: ("✖", "#e06666"),
    UNKNOWN: ("…", "#999999"),
}


class _CallThread(QThread):
    line = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.fn(self.line.emit)
        except Exception as exc:
            log.warning("AutoDub setup step failed", exc_info=True)
            self.error.emit(str(exc))


class FixRunner(QObject):
    """Runs Fix.steps one after another; emits line(text) and done(ok, message)."""

    line = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, fix, parent=None):
        super().__init__(parent)
        self.fix = fix
        self.index = -1
        self.current = None
        self._failed = ""

    def start(self):
        self._next()

    def cancel(self):
        self._failed = "Отменено"
        if isinstance(self.current, WorkerProcess):
            self.current.kill()

    def _next(self):
        if self._failed:
            self.done.emit(False, self._failed)
            return
        self.index += 1
        if self.index >= len(self.fix.steps):
            try:
                if self.fix.after:
                    self.fix.after()
            except Exception as exc:
                self.done.emit(False, str(exc))
                return
            self.done.emit(True, "Готово: " + self.fix.label)
            return
        step = self.fix.steps[self.index]
        self.line.emit("▶ [%d/%d] %s" % (self.index + 1, len(self.fix.steps), step["label"]))
        if step["type"] == "call":
            t = _CallThread(step["fn"], self)
            t.line.connect(self.line)
            t.error.connect(self._on_error)
            t.finished.connect(self._next)
            self.current = t
            t.start()
        else:
            if not step.get("python") or not os.path.isfile(step["python"]):
                self._failed = "Не найден Python: %s" % (step.get("python") or "—")
                QTimer.singleShot(0, self._next)
                return
            w = WorkerProcess(step["args"], self, python=step["python"], script=step.get("script") or "",
                              expect_result=step.get("expect_result", False))
            w.output.connect(self.line)
            w.progress.connect(lambda v, m: self.line.emit("   %3d%% %s" % (int(v * 100), m)))
            w.failed.connect(self._on_error)
            w.finished.connect(self._next)
            self.current = w
            w.start()

    def _on_error(self, message):
        self._failed = message or "Ошибка"
        self.line.emit("✖ " + self._failed)


class SetupDialog(QDialog):
    def __init__(self, parent=None, languages=None, focus_group=None, reason=""):
        super().__init__(parent)
        self.setWindowTitle("AutoDub — настройка и проверка")
        self.resize(900, 720)
        self.doctor = Doctor.instance()
        self.languages = languages or self.doctor.languages
        self.focus_group = focus_group
        self.runner = None
        self._build(reason)
        self.doctor.updated.connect(self.refresh_checks)
        self.doctor.finished.connect(self.refresh_checks)
        self.recheck()

    # ------------------------------------------------------------------ UI --
    def _build(self, reason):
        root = QVBoxLayout(self)
        if reason:
            lab = QLabel(reason)
            lab.setWordWrap(True)
            lab.setStyleSheet("color:#e69138; font-weight:bold;")
            root.addWidget(lab)

        cfg = config()
        box = QGroupBox("Пути и модели (сохраняются в ~/.openshot_qt/autodub.json)")
        form = QFormLayout(box)
        self.edits = {}

        def path_row(key, label, folder=False):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(str(cfg.get(key, "")))
            edit.editingFinished.connect(functools.partial(self._edited, key))
            btn = QPushButton("…")
            btn.setMaximumWidth(30)
            btn.clicked.connect(functools.partial(self._browse, key, folder))
            h.addWidget(edit, 1)
            h.addWidget(btn)
            self.edits[key] = edit
            form.addRow(label, row)

        def combo_row(key, label, values):
            combo = QComboBox()
            for value, name in values:
                combo.addItem(name, value)
            idx = combo.findData(cfg.get(key))
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.currentIndexChanged.connect(functools.partial(self._combo, key, combo))
            form.addRow(label, combo)

        path_row("asr_python", "Python распознавания/перевода")
        combo_row("whisper_model", "Модель Whisper", [(m, m) for m in WHISPER_MODELS])
        combo_row("whisper_device", "Устройство Whisper", [("cuda", "CUDA (GPU)"), ("cpu", "CPU")])
        combo_row("tts_engine", "Движок озвучки", [("local", "CosyVoice в плагине (без ComfyUI)"),
                                                   ("comfyui", "Сервер ComfyUI")])
        path_row("tts_python", "Python озвучки (torch)")
        path_row("cosyvoice_src", "Код CosyVoice (cosyvoice/, matcha/)", folder=True)
        path_row("cosyvoice_models_dir", "Папка моделей CosyVoice", folder=True)
        combo_row("cosyvoice_model", "Модель CosyVoice", [(m, m) for m in COSYVOICE_MODELS])
        combo_row("tts_mode", "Режим клонирования", [("auto", "auto"), ("cross_lingual", "cross-lingual"),
                                                     ("zero_shot", "zero-shot")])
        fp16 = QCheckBox("fp16 (меньше видеопамяти)")
        fp16.setChecked(bool(cfg.get("tts_fp16")))
        fp16.toggled.connect(lambda v: self._set("tts_fp16", bool(v)))
        form.addRow("", fp16)
        root.addWidget(box)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Компонент", "Состояние", ""])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(True)
        root.addWidget(self.table, 2)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setPlaceholderText("Здесь будет лог установки и скачивания")
        root.addWidget(self.log, 1)

        row = QHBoxLayout()
        self.summary = QLabel("")
        row.addWidget(self.summary, 1)
        self.btn_cancel = QPushButton("Остановить")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_fix)
        row.addWidget(self.btn_cancel)
        btn = QPushButton("Проверить снова")
        btn.clicked.connect(self.recheck)
        row.addWidget(btn)
        btn = QPushButton("Закрыть")
        btn.clicked.connect(self.accept)
        row.addWidget(btn)
        root.addLayout(row)

    # ------------------------------------------------------------ config --
    def _set(self, key, value):
        cfg = config()
        if cfg.get(key) != value:
            cfg.set(key, value)
            cfg.save()
            QTimer.singleShot(200, self.recheck)

    def _edited(self, key):
        self._set(key, self.edits[key].text().strip().strip('"'))

    def _combo(self, key, combo, *_):
        self._set(key, combo.currentData())

    def _browse(self, key, folder):
        current = self.edits[key].text()
        if folder:
            path = QFileDialog.getExistingDirectory(self, "Папка", current or os.path.expanduser("~"))
        else:
            path, _ = QFileDialog.getOpenFileName(self, "python.exe", current or os.path.expanduser("~"),
                                                  "Python (python.exe python*);;Все файлы (*)")
        if path:
            path = os.path.normpath(path)
            self.edits[key].setText(path)
            self._set(key, path)

    # ------------------------------------------------------------ checks --
    def recheck(self):
        if self.runner is None:
            self.doctor.run_all(self.languages)
        self.refresh_checks()

    def refresh_checks(self):
        checks = sorted(self.doctor.checks.values(),
                        key=lambda c: ({"asr": 0, "translate": 1, "tts": 2}.get(c.group, 3),
                                       0 if c.group == self.focus_group else 1))
        self.table.setRowCount(len(checks))
        for row, c in enumerate(checks):
            icon, color = STATUS_VIEW.get(c.status, ("?", "#999"))
            it = QTableWidgetItem(icon)
            it.setForeground(QColor(color))
            it.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 0, it)
            title = QTableWidgetItem(c.title + ("" if c.blocking else "  (необяз.)"))
            if c.group == self.focus_group:
                font = title.font()
                font.setBold(True)
                title.setFont(font)
            self.table.setItem(row, 1, title)
            detail = QTableWidgetItem(c.detail)
            detail.setToolTip(c.detail)
            self.table.setItem(row, 2, detail)
            self.table.setCellWidget(row, 3, self._actions(c))
        self.table.resizeRowsToContents()
        status, text = self.doctor.summary()
        self.summary.setText(text)
        self.summary.setStyleSheet("color:%s;" % STATUS_VIEW.get(status, ("", "#999"))[1])

    def _actions(self, check):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        for fix in check.fixes:
            b = QPushButton(fix.label)
            b.setEnabled(self.runner is None)
            b.clicked.connect(functools.partial(self._run_fix, fix))
            h.addWidget(b)
        if check.help_text:
            b = QPushButton("Инструкция")
            b.clicked.connect(functools.partial(self._show_help, check))
            h.addWidget(b)
        h.addStretch(1)
        return w

    def _show_help(self, check):
        self.log.appendPlainText("── %s ──\n%s\n" % (check.title, check.help_text))
        QMessageBox.information(self, check.title, check.help_text)

    # -------------------------------------------------------------- fixes --
    def _run_fix(self, fix):
        if self.runner is not None:
            return
        if fix.confirm and QMessageBox.question(self, fix.label, fix.confirm) != QMessageBox.Yes:
            return
        self.log.appendPlainText("═══ %s ═══" % fix.label)
        self.runner = FixRunner(fix, self)
        self.runner.line.connect(self.log.appendPlainText)
        self.runner.done.connect(self._fix_done)
        self.btn_cancel.setEnabled(True)
        self.refresh_checks()
        self.runner.start()

    def _cancel_fix(self):
        if self.runner:
            self.runner.cancel()

    def _fix_done(self, ok, message):
        self.log.appendPlainText(("✔ " if ok else "✖ ") + message + "\n")
        self.runner = None
        self.btn_cancel.setEnabled(False)
        cfg = config()
        cfg.load()
        for key, edit in self.edits.items():
            edit.setText(str(cfg.get(key, "")))
        self.recheck()
