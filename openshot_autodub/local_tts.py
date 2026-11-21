"""
 @file
 @brief Local CosyVoice engine: keeps src/dubbing_worker/cosyvoice_tts.py alive
        in the CosyVoice Python (e.g. C:\\ComfyUI\\venv) so the model is loaded
        once, and feeds it phrase jobs one by one. No ComfyUI server needed.

 Signals mirror ComfyDubber so the panel can use either backend.
"""

import json
import os
import time

from qt_api import QObject, QProcess, QProcessEnvironment, QApplication, pyqtSignal

from classes.logger import log
from .config import WORKERS_DIR, LOCAL_MODELS, LOCAL_VENDOR, setting

DEFAULT_TTS_PYTHON = ""
DEFAULT_COSYVOICE_SRC = LOCAL_VENDOR
DEFAULT_MODELS_DIR = LOCAL_MODELS


def tts_script_path():
    return os.path.join(WORKERS_DIR, "cosyvoice_tts.py")


def tts_python():
    return str(setting("tts_python", DEFAULT_TTS_PYTHON))


def tts_args():
    args = [
        tts_script_path(),
        "--cosyvoice-src", str(setting("cosyvoice_src", DEFAULT_COSYVOICE_SRC)),
        "--models-dir", str(setting("cosyvoice_models_dir", DEFAULT_MODELS_DIR)),
        "--model", str(setting("cosyvoice_model", "Fun-CosyVoice3-0.5B")),
    ]
    if setting("tts_fp16", False):
        args.append("--fp16")
    return args


class LocalTtsEngine(QObject):
    segment_started = pyqtSignal(str)
    segment_done = pyqtSignal(str, str, float, float)   # seg_id, path, duration, speed
    segment_failed = pyqtSignal(str, str)
    queue_progress = pyqtSignal(int, int)
    finished = pyqtSignal()
    status = pyqtSignal(str)

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = LocalTtsEngine(QApplication.instance())
        return cls._instance

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self.ready = False
        self.queue = []
        self.current = None
        self.done_count = 0
        self.total = 0
        self._buffer = b""
        self._stderr_tail = []
        self._launch_args = None
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    # ------------------------------------------------------------ public --
    def isRunning(self):
        return bool(self.current or self.queue)

    def start_jobs(self, jobs):
        for job in jobs:
            self.queue.append(dict(job))
        self.total += len(jobs)
        self._ensure_process()
        self._pump()

    def cancel(self):
        """Drop queued jobs; the phrase being generated right now still finishes."""
        dropped = self.queue
        self.queue = []
        for job in dropped:
            self.segment_failed.emit(job["seg_id"], "Отменено")
        if not self.current:
            self._batch_finished()

    def shutdown(self):
        if self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning:
            try:
                self.proc.write(b'{"cmd": "quit"}\n')
                if not self.proc.waitForFinished(1500):
                    self.proc.kill()
            except Exception:
                pass
        self.proc = None
        self.ready = False

    # ----------------------------------------------------------- process --
    def _ensure_process(self):
        args = tts_args()
        python = tts_python()
        running = self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning
        if running and self._launch_args == (python, args):
            return
        if running:
            self.shutdown()
        if not os.path.isfile(python):
            self._fail_all("Не найден Python для CosyVoice: %s\n"
                           "AutoDub → «Настройка и проверка»" % python)
            return
        self.proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONUNBUFFERED", "1")
        self.proc.setProcessEnvironment(env)
        self.proc.readyReadStandardOutput.connect(self._read_stdout)
        self.proc.readyReadStandardError.connect(self._read_stderr)
        self.proc.finished.connect(self._on_exit)
        self.proc.errorOccurred.connect(self._on_proc_error)
        self.ready = False
        self._buffer = b""
        self._launch_args = (python, args)
        log.info("Dubbing TTS: %s %s", python, " ".join(args))
        self.status.emit("Загрузка модели CosyVoice…")
        self.proc.start(python, args)

    def _read_stdout(self):
        self._buffer += bytes(self.proc.readAllStandardOutput())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("@@"):
                try:
                    self._handle(json.loads(line[2:]))
                except ValueError:
                    pass
            elif line:
                log.info("[cosyvoice_tts] %s", line)

    def _read_stderr(self):
        text = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        for line in text.splitlines():
            if line.strip():
                log.info("[cosyvoice_tts] %s", line)
                self._stderr_tail.append(line)
        self._stderr_tail = self._stderr_tail[-20:]

    def _handle(self, msg):
        kind = msg.get("type")
        if kind == "ready":
            self.ready = True
            self.status.emit("CosyVoice готов (%s, %s c)" % (msg.get("device"), msg.get("load_seconds")))
            self._pump()
        elif kind == "started":
            self.segment_started.emit(str(msg.get("id")))
        elif kind == "done":
            self.current = None
            self.done_count += 1
            self.segment_done.emit(str(msg["id"]), msg["path"], float(msg.get("duration") or 0.0),
                                   float(msg.get("speed") or 1.0))
            self._advance()
        elif kind == "failed":
            self.current = None
            self.done_count += 1
            self.segment_failed.emit(str(msg.get("id")), str(msg.get("message")))
            self._advance()
        elif kind == "log":
            self.status.emit(str(msg.get("message", "")))
        elif kind == "error":
            self._fail_all(str(msg.get("message")))

    def _advance(self):
        self.queue_progress.emit(self.done_count, self.total)
        if self.queue:
            self._pump()
        else:
            self._batch_finished()

    def _pump(self):
        if not self.ready or self.current or not self.queue or self.proc is None:
            return
        job = self.queue.pop(0)
        self.current = job
        payload = {
            "id": job["seg_id"], "text": job["text"], "ref": job["ref_path"],
            "ref_text": job.get("ref_text", ""), "ref_lang": job.get("ref_lang", ""),
            "lang": job.get("language", ""), "speed": job.get("speed", 1.0),
            "seed": int(setting("seed", 42) or 42),
            "out": os.path.join(job["out_dir"], "%s.wav" % job["seg_id"]),
            "slot": job.get("slot", 0.0), "fit_speed": bool(job.get("fit_speed", True)),
            "mode": str(setting("tts_mode", "auto") or "auto"),
        }
        # unique file per generation so OpenShot never shows a cached old waveform
        stem, ext = os.path.splitext(payload["out"])
        payload["out"] = "%s_%d%s" % (stem, int(time.time() * 1000) % 100000000, ext)
        self.proc.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

    def _batch_finished(self):
        self.done_count = 0
        self.total = 0
        self.finished.emit()

    def _fail_all(self, message):
        jobs = ([self.current] if self.current else []) + self.queue
        self.current = None
        self.queue = []
        for job in jobs:
            self.segment_failed.emit(job["seg_id"], message)
        if not jobs:
            self.status.emit(message.splitlines()[0])
        self._batch_finished()

    def _on_proc_error(self, err):
        if err == QProcess.ProcessError.FailedToStart:
            self.ready = False
            self.proc = None
            self._fail_all("Не удалось запустить Python для CosyVoice: %s" % tts_python())

    def _on_exit(self, *_args):
        was_ready = self.ready
        self.ready = False
        if self.current or self.queue:
            tail = "\n".join(self._stderr_tail[-8:])
            self._fail_all(("Процесс CosyVoice завершился" if was_ready else
                            "CosyVoice не запустился") + ":\n" + tail)
