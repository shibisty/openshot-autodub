"""
 @file
 @brief QProcess wrapper around src/dubbing_worker/dub_worker.py running in an
        external Python (venv with faster-whisper / argostranslate).
"""

import json
import os
import sys

from qt_api import QObject, QProcess, QProcessEnvironment, pyqtSignal

from classes.logger import log

from .config import WORKERS_DIR, setting


def worker_script_path():
    return os.path.join(WORKERS_DIR, "dub_worker.py")


def worker_python():
    return str(setting("asr_python", ""))


class WorkerProcess(QObject):
    """One worker command. Emits progress(value 0..1, message), result(dict), failed(str)."""

    progress = pyqtSignal(float, str)
    result = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()
    output = pyqtSignal(str)   # every plain stdout/stderr line (for setup logs)

    def __init__(self, args, parent=None, python=None, script=None, expect_result=True):
        super().__init__(parent)
        self.args = list(args)
        self.python = python
        self.script = script
        self.expect_result = expect_result
        self.proc = QProcess(self)
        self._buffer = b""
        self._stderr_tail = []
        self._got_result = False
        self._error = ""
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONUTF8", "1")
        token = setting("hf_token", "")
        env.insert("PYTHONNOUSERSITE", "1")
        if token:
            env.insert("HF_TOKEN", token)
        self.proc.setProcessEnvironment(env)
        self.proc.readyReadStandardOutput.connect(self._read_stdout)
        self.proc.readyReadStandardError.connect(self._read_stderr)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)

    def start(self):
        python = self.python or worker_python()
        script = worker_script_path() if self.script is None else self.script  # "" = plain python -m ...
        log.info("Dubbing worker: %s %s %s", python, script, " ".join(self.args))
        self.proc.start(python, ([script] if script else []) + self.args)

    def kill(self):
        if self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()

    def is_running(self):
        return self.proc.state() != QProcess.ProcessState.NotRunning

    def _read_stdout(self):
        self._buffer += bytes(self.proc.readAllStandardOutput())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._handle_line(line.decode("utf-8", "replace").strip())

    def _read_stderr(self):
        text = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        for line in text.splitlines():
            if line.strip():
                log.info("[dub_worker] %s", line)
                self.output.emit(line)
                self._stderr_tail.append(line)
        self._stderr_tail = self._stderr_tail[-15:]

    def _handle_line(self, line):
        if not line.startswith("@@"):
            if line:
                log.info("[dub_worker] %s", line)
                self.output.emit(line)
            return
        try:
            msg = json.loads(line[2:])
        except ValueError:
            return
        kind = msg.get("type")
        if kind == "progress":
            self.progress.emit(float(msg.get("value", 0.0)), str(msg.get("message", "")))
        elif kind == "result":
            self._got_result = True
            self.result.emit(msg)
        elif kind == "error":
            self._error = str(msg.get("message", "error"))

    def _on_error(self, err):
        if err == QProcess.ProcessError.FailedToStart:
            # QProcess emits no finished() in this case
            self.failed.emit(self._error or (
                "Не удалось запустить: %s\n"
                "Откройте AutoDub → «Настройка и проверка»." % (self.python or worker_python())))
            self.finished.emit()

    def _on_finished(self, *_args):
        if self._buffer:
            self._handle_line(self._buffer.decode("utf-8", "replace").strip())
            self._buffer = b""
        if not self.expect_result and not self._error:
            code = self.proc.exitCode()
            if self.proc.exitStatus() == QProcess.ExitStatus.NormalExit and code == 0:
                self._got_result = True
                self.result.emit({"type": "result", "exit_code": 0})
            else:
                self._error = "Команда завершилась с кодом %s" % code
        if not self._got_result:
            tail = "\n".join(self._stderr_tail[-6:])
            self.failed.emit(self._error or ("Воркер завершился без результата.\n" + tail))
        self.finished.emit()
