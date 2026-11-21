"""
Environment checks ("doctor") and the fix actions offered for each problem.

Checks are grouped by the panel action they gate:
    asr        - "Распознать"   (Python + faster-whisper; Whisper model, CUDA are warnings)
    translate  - "Перевести"    (argostranslate; language pack is a warning: installed on demand)
    tts        - "Озвучить"     (CosyVoice Python + code + model, or ComfyUI server)
"""

import glob
import io
import os
import shutil
import subprocess
import sys
import zipfile
from urllib.request import Request, urlopen

from qt_api import QObject, pyqtSignal

from classes.logger import log

from .config import (
    COSYVOICE_MODELS, COSYVOICE_REPO_ZIP, LOCAL_ASR_ENV, LOCAL_TTS_ENV, LOCAL_VENDOR, PLUGIN_ROOT,
    WORKERS_DIR, config, cosyvoice_model_dir, setting, venv_python,
)
from .worker_process import WorkerProcess

OK, WARN, MISSING, UNKNOWN, RUNNING = "ok", "warn", "missing", "unknown", "running"

COSYVOICE_GIT = "https://github.com/filliptm/ComfyUI_FL-CosyVoice3.git"
REQ_ASR = os.path.join(PLUGIN_ROOT, "requirements-asr.txt")
REQ_TTS = os.path.join(PLUGIN_ROOT, "requirements-tts.txt")
TORCH_INDEX = "https://download.pytorch.org/whl/cu126"
NVIDIA_LIBS = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]


class Check(object):
    def __init__(self, key, group, title, status=UNKNOWN, detail="", blocking=True, fixes=None, help_text=""):
        self.key, self.group, self.title = key, group, title
        self.status, self.detail, self.blocking = status, detail, blocking
        self.fixes = fixes or []          # [Fix]
        self.help_text = help_text


class Fix(object):
    """A list of steps; step = dict(type='proc', python, args, script=None, expect_result, label)
    or dict(type='call', fn, label). `after` (callable) runs on success (e.g. save a config path)."""

    def __init__(self, label, steps, after=None, confirm=""):
        self.label, self.steps, self.after, self.confirm = label, steps, after, confirm


def proc(label, python, args, script=None, expect_result=False):
    return {"type": "proc", "label": label, "python": python, "args": list(args), "script": script,
            "expect_result": expect_result}


def call(label, fn):
    return {"type": "call", "label": label, "fn": fn}


# ------------------------------------------------------------------ helpers --

def find_base_python():
    """A Windows CPython 3.10-3.12 x64 to create virtual environments from (not MSYS2's)."""
    candidates = []
    if shutil.which("py"):
        for ver in ("3.12", "3.11", "3.10"):
            candidates.append(["py", "-%s" % ver])
    for path in (r"C:\Python312\python.exe", r"C:\Python311\python.exe", r"C:\Python310\python.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python312\python.exe"),
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311\python.exe")):
        if os.path.isfile(path):
            candidates.append([path])
    which = shutil.which("python")
    if which and "msys" not in which.lower():
        candidates.append([which])
    probe = "import sys,sysconfig;print(sys.executable);print('%d.%d' % sys.version_info[:2]);print(sysconfig.get_platform())"
    for cmd in candidates:
        try:
            out = subprocess.run(cmd + ["-c", probe], capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.split()
        except Exception:
            continue
        if len(out) >= 3 and out[1] in ("3.10", "3.11", "3.12") and out[2] in ("win-amd64", "win_amd64"):
            return out[0]
    return ""


def download_cosyvoice_code(log_cb):
    """Download ComfyUI_FL-CosyVoice3 (only its cosyvoice/ and matcha/ packages are used)."""
    log_cb("GET " + COSYVOICE_REPO_ZIP)
    try:
        req = Request(COSYVOICE_REPO_ZIP, headers={"User-Agent": "openshot-autodub"})
        with urlopen(req, timeout=120) as resp:
            data = resp.read()
    except Exception as exc:
        git = shutil.which("git")
        if not git:
            raise RuntimeError("Не удалось скачать архив (%s), и git не найден" % exc)
        log_cb("Архив недоступен (%s), пробую git clone…" % exc)
        shutil.rmtree(LOCAL_VENDOR, ignore_errors=True)
        os.makedirs(os.path.dirname(LOCAL_VENDOR), exist_ok=True)
        out = subprocess.run([git, "clone", "--depth", "1", COSYVOICE_GIT, LOCAL_VENDOR],
                             capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if out.returncode != 0 or not os.path.isdir(os.path.join(LOCAL_VENDOR, "cosyvoice")):
            raise RuntimeError("git clone не удался: %s" % (out.stderr or out.stdout)[-400:])
        log_cb("Код CosyVoice: " + LOCAL_VENDOR)
        return
    log_cb("Скачано %.1f МБ, распаковка…" % (len(data) / 1e6))
    tmp = LOCAL_VENDOR + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(tmp)
    inner = [p for p in glob.glob(os.path.join(tmp, "*")) if os.path.isdir(p)]
    if not inner or not os.path.isdir(os.path.join(inner[0], "cosyvoice")):
        raise RuntimeError("В архиве нет папки cosyvoice/")
    shutil.rmtree(LOCAL_VENDOR, ignore_errors=True)
    os.makedirs(os.path.dirname(LOCAL_VENDOR), exist_ok=True)
    shutil.move(inner[0], LOCAL_VENDOR)
    shutil.rmtree(tmp, ignore_errors=True)
    log_cb("Код CosyVoice: " + LOCAL_VENDOR)


def argos_has(pairs, src, dst):
    pairs = set(pairs or [])
    if src == dst:
        return True
    return ("%s:%s" % (src, dst)) in pairs or (("%s:en" % src) in pairs and ("en:%s" % dst) in pairs)


# ------------------------------------------------------------------- doctor --

class Doctor(QObject):
    updated = pyqtSignal()
    finished = pyqtSignal()

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = Doctor()
        return cls._instance

    def __init__(self, parent=None):
        super().__init__(parent)
        self.checks = {}
        self.asr_info = None
        self.tts_info = None
        self.languages = ("en", "ru")
        self._pending = 0
        self._procs = []
        self.has_run = False

    # ------------------------------------------------------------ results --
    def blocking(self, group):
        return [c for c in self.checks.values()
                if c.group == group and c.blocking and c.status in (MISSING, UNKNOWN)]

    def running(self):
        return self._pending > 0

    def summary(self):
        if self.running():
            return RUNNING, "Проверка окружения…"
        missing = [c for c in self.checks.values() if c.blocking and c.status == MISSING]
        warns = [c for c in self.checks.values() if c.status == WARN or (not c.blocking and c.status == MISSING)]
        if missing:
            return MISSING, "Не готово: " + ", ".join(c.title for c in missing[:3]) + ("…" if len(missing) > 3 else "")
        if warns:
            return WARN, "Готово, есть предупреждения: " + ", ".join(c.title for c in warns[:2])
        return OK, "Окружение готово"

    def _set(self, check):
        self.checks[check.key] = check
        self.updated.emit()

    # --------------------------------------------------------------- run --
    def run_all(self, languages=None):
        if self.running():
            return
        if languages:
            self.languages = languages
        self.has_run = True
        self.checks = {}
        cfg = config()
        self._check_asr_python()
        self._check_tts_static()
        if setting("tts_engine", "local") == "comfyui":
            self._check_comfy()
        asr_py = setting("asr_python", "")
        if os.path.isfile(asr_py):
            self._start(WorkerProcess(
                ["check", "--whisper-model", str(setting("whisper_model", "medium"))], self, python=asr_py),
                self._on_asr_info, "asr")
        tts_py = setting("tts_python", "")
        if setting("tts_engine", "local") == "local" and os.path.isfile(tts_py):
            self._start(WorkerProcess(
                ["--check", "--cosyvoice-src", str(setting("cosyvoice_src", "")),
                 "--models-dir", str(setting("cosyvoice_models_dir", "")),
                 "--model", str(setting("cosyvoice_model", "Fun-CosyVoice3-0.5B"))],
                self, python=tts_py, script=os.path.join(WORKERS_DIR, "cosyvoice_tts.py")),
                self._on_tts_info, "tts")
        del cfg
        self.updated.emit()
        if not self._pending:
            self.finished.emit()

    def _start(self, worker, on_result, group):
        self._pending += 1
        self._procs.append(worker)
        worker.result.connect(on_result)
        worker.failed.connect(lambda msg, g=group: self._on_probe_failed(g, msg))
        worker.finished.connect(lambda w=worker: self._probe_done(w))
        worker.start()

    def _probe_done(self, worker):
        self._pending = max(0, self._pending - 1)
        if worker in self._procs:
            self._procs.remove(worker)
        self.updated.emit()
        if not self._pending:
            self.finished.emit()

    def _on_probe_failed(self, group, msg):
        key = "asr_packages" if group == "asr" else "tts_packages"
        title = "Пакеты распознавания" if group == "asr" else "Пакеты CosyVoice"
        self._set(Check(key, "asr" if group == "asr" else "tts", title, MISSING,
                        "Python не отвечает: " + msg.splitlines()[0][:200],
                        fixes=self._asr_package_fixes() if group == "asr" else self._tts_package_fixes()))

    # ------------------------------------------------------------- ASR --
    def _check_asr_python(self):
        py = setting("asr_python", "")
        if os.path.isfile(py):
            self._set(Check("asr_python", "asr", "Python для распознавания", OK, py))
            return
        self._set(Check(
            "asr_python", "asr", "Python для распознавания", MISSING,
            "Не найден: %s" % (py or "не задан"),
            fixes=[self._create_env_fix("asr")],
            help_text=("Нужен Windows Python 3.10–3.12 с faster-whisper и argostranslate.\n"
                       "Автоматически: кнопка «Создать окружение» (envs\\asr в папке плагина).\n"
                       "Вручную:\n  py -3.12 -m venv %s\n  %s -m pip install -r %s\n"
                       "и укажите путь к python.exe выше.") % (LOCAL_ASR_ENV, venv_python(LOCAL_ASR_ENV), REQ_ASR)))
        for key, title, group in (("asr_packages", "Пакеты распознавания", "asr"),
                                  ("translate_packages", "Пакет перевода (Argos)", "translate")):
            self._set(Check(key, group, title, MISSING, "Сначала нужен Python"))

    def _asr_package_fixes(self):
        py = setting("asr_python", "")
        return [Fix("Установить пакеты", [proc("pip install -r requirements-asr.txt", py,
                                                ["-m", "pip", "install", "-r", REQ_ASR])])]

    def _on_asr_info(self, info):
        self.asr_info = info
        py = setting("asr_python", "")
        mods = info.get("modules") or {}
        need = [m for m in ("numpy", "soundfile", "faster_whisper", "ctranslate2", "onnxruntime") if not mods.get(m)]
        self._set(Check("asr_packages", "asr", "Пакеты распознавания", MISSING if need else OK,
                        ("Нет: " + ", ".join(need)) if need else "Python %s, faster-whisper" % info.get("version"),
                        fixes=self._asr_package_fixes() if need else []))
        self._set(Check("translate_packages", "translate", "Пакет перевода (Argos)",
                        OK if mods.get("argostranslate") else MISSING,
                        "argostranslate" if mods.get("argostranslate") else "Нет модуля argostranslate",
                        fixes=[] if mods.get("argostranslate") else [
                            Fix("Установить argostranslate", [proc("pip install argostranslate", py,
                                                                   ["-m", "pip", "install", "argostranslate"])])]))
        if mods.get("faster_whisper"):
            model = info.get("whisper_model") or setting("whisper_model", "medium")
            cached = bool(info.get("whisper_cached"))
            self._set(Check("whisper_model", "asr", "Модель Whisper %s" % model, OK if cached else WARN,
                            "Скачана" if cached else "Не скачана — скачается при первом распознавании",
                            blocking=False,
                            fixes=[] if cached else [Fix("Скачать сейчас", [proc(
                                "download-whisper " + model, py, ["download-whisper", "--model", model],
                                script=os.path.join(WORKERS_DIR, "dub_worker.py"), expect_result=True)])]))
            if setting("whisper_device", "cuda") == "cuda":
                cuda = int(info.get("cuda_devices") or 0)
                self._set(Check("whisper_cuda", "asr", "CUDA для Whisper", OK if cuda else WARN,
                                ("GPU: %d" % cuda) if cuda else "GPU не найден — распознавание пойдёт на CPU (медленнее)",
                                blocking=False,
                                fixes=[] if cuda else [Fix("Установить cuBLAS/cuDNN", [proc(
                                    "pip install nvidia-cublas/cudnn", py, ["-m", "pip", "install"] + NVIDIA_LIBS)])],
                                help_text="Нужны драйвер NVIDIA и пакеты %s в этом venv." % " ".join(NVIDIA_LIBS)))
        if mods.get("argostranslate"):
            src, dst = self.languages
            src = src if src and src != "auto" else "en"
            pairs = info.get("argos_pairs") or []
            have = argos_has(pairs, src, dst)
            self._set(Check("argos_pair", "translate", "Языковой пакет %s→%s" % (src, dst), OK if have else WARN,
                            "Установлен" if have else "Не установлен — скачается при первом переводе",
                            blocking=False,
                            fixes=[] if have else [Fix("Скачать сейчас", [proc(
                                "install-argos %s→%s" % (src, dst), py,
                                ["install-argos", "--source", src, "--target", dst],
                                script=os.path.join(WORKERS_DIR, "dub_worker.py"), expect_result=True)])]))

    # ------------------------------------------------------------- TTS --
    def _check_tts_static(self):
        if setting("tts_engine", "local") != "local":
            return
        py = setting("tts_python", "")
        if os.path.isfile(py):
            self._set(Check("tts_python", "tts", "Python для озвучки (torch)", OK, py))
        else:
            self._set(Check(
                "tts_python", "tts", "Python для озвучки (torch)", MISSING, "Не найден: %s" % (py or "не задан"),
                fixes=[self._create_env_fix("tts")],
                help_text=("Нужен Python с torch (CUDA) и зависимостями CosyVoice. Подойдёт venv ComfyUI, где\n"
                           "работает нода FL CosyVoice3 (например C:\\ComfyUI\\venv\\Scripts\\python.exe).\n"
                           "Автоматически: «Создать окружение» (~5 ГБ: torch CUDA + зависимости).\n"
                           "Вручную:\n  py -3.12 -m venv %s\n  %s -m pip install torch torchaudio --index-url %s\n"
                           "  %s -m pip install -r %s") % (LOCAL_TTS_ENV, venv_python(LOCAL_TTS_ENV), TORCH_INDEX,
                                                           venv_python(LOCAL_TTS_ENV), REQ_TTS)))
        src = setting("cosyvoice_src", "")
        src_ok = os.path.isdir(os.path.join(src, "cosyvoice")) and os.path.isdir(os.path.join(src, "matcha"))
        self._set(Check(
            "cosyvoice_src", "tts", "Код CosyVoice", OK if src_ok else MISSING,
            src if src_ok else "Нет cosyvoice/ и matcha/ в %s" % (src or "—"),
            fixes=[] if src_ok else [Fix("Скачать с GitHub", [call("Скачивание ComfyUI_FL-CosyVoice3",
                                                                   download_cosyvoice_code)],
                                         after=lambda: self._save("cosyvoice_src", LOCAL_VENDOR))],
            help_text=("Используются только пакеты cosyvoice/ и matcha/ из\n"
                       "https://github.com/filliptm/ComfyUI_FL-CosyVoice3 (ComfyUI не запускается).\n"
                       "Если нода уже стоит в ComfyUI — укажите её папку выше.")))
        model = setting("cosyvoice_model", "Fun-CosyVoice3-0.5B")
        mdir = cosyvoice_model_dir()
        repo = COSYVOICE_MODELS.get(model, "")
        self._set(Check(
            "cosyvoice_model", "tts", "Модель %s" % model, OK if mdir else MISSING,
            mdir or "Не найдена в %s" % setting("cosyvoice_models_dir", ""),
            fixes=[] if mdir or not os.path.isfile(py) else [Fix("Скачать (~4 ГБ)", [proc(
                "download " + repo, py,
                ["--models-dir", str(setting("cosyvoice_models_dir", "")), "--model", model,
                 "--download-model", repo],
                script=os.path.join(WORKERS_DIR, "cosyvoice_tts.py"), expect_result=True)])],
            help_text=("Модель скачивается с HuggingFace (%s) в папку моделей выше.\n"
                       "Если модель уже скачана нодой ComfyUI — укажите C:\\ComfyUI\\models\\cosyvoice.") % repo))
        if not os.path.isfile(py):
            self._set(Check("tts_packages", "tts", "Пакеты CosyVoice", MISSING, "Сначала нужен Python"))

    def _tts_package_fixes(self):
        py = setting("tts_python", "")
        return [Fix("Установить зависимости CosyVoice", [proc("pip install -r requirements-tts.txt", py,
                                                              ["-m", "pip", "install", "-r", REQ_TTS])],
                    confirm="Пакеты будут поставлены в %s.\nЕсли это venv ComfyUI, его версии пакетов могут "
                            "измениться. Продолжить?" % py)]

    def _on_tts_info(self, info):
        self.tts_info = info
        mods = info.get("modules") or {}
        if not mods.get("torch"):
            self._set(Check("tts_packages", "tts", "Пакеты CosyVoice", MISSING, "Нет torch",
                            fixes=[Fix("Установить torch (CUDA) и зависимости", [
                                proc("pip install torch torchaudio (cu126)", setting("tts_python", ""),
                                     ["-m", "pip", "install", "torch", "torchaudio", "--index-url", TORCH_INDEX]),
                                proc("pip install -r requirements-tts.txt", setting("tts_python", ""),
                                     ["-m", "pip", "install", "-r", REQ_TTS])])]))
            return
        imported = info.get("cosyvoice_import")
        missing = [m for m, ok in mods.items() if not ok]
        if imported:
            status, detail = OK, "torch %s, %s" % (info.get("torch"), info.get("gpu") or "CPU")
        elif imported is None:
            status, detail = UNKNOWN, "Не проверено (нет кода CosyVoice)"
        else:
            status = MISSING
            detail = info.get("cosyvoice_error") or ("Нет: " + ", ".join(missing))
        self._set(Check("tts_packages", "tts", "Пакеты CosyVoice", status, detail,
                        fixes=self._tts_package_fixes() if status == MISSING else []))
        self._set(Check("tts_cuda", "tts", "CUDA для озвучки", OK if info.get("cuda") else WARN,
                        info.get("gpu") or "torch без CUDA — озвучка будет очень медленной", blocking=False))

    def _check_comfy(self):
        url = str(setting("comfy_url", "http://127.0.0.1:8188")).rstrip("/")
        try:
            with urlopen(url + "/system_stats", timeout=1.5):
                pass
            self._set(Check("comfy", "tts", "Сервер ComfyUI", OK, url))
        except Exception as exc:
            self._set(Check("comfy", "tts", "Сервер ComfyUI", MISSING, "%s недоступен (%s)" % (url, exc),
                            help_text="Запустите ComfyUI с нодами FL CosyVoice3 или переключите движок на «local»."))

    # ----------------------------------------------------------- shared --
    @staticmethod
    def _save(key, value):
        cfg = config()
        cfg.set(key, value)
        cfg.save()

    def _create_env_fix(self, kind):
        env = LOCAL_ASR_ENV if kind == "asr" else LOCAL_TTS_ENV
        py = venv_python(env)

        def make_venv(log_cb):
            base = find_base_python()
            if not base:
                raise RuntimeError("Не найден Windows Python 3.10–3.12 x64. Установите его с python.org "
                                   "(или `winget install Python.Python.3.12`) и повторите.")
            log_cb("Базовый Python: " + base)
            out = subprocess.run([base, "-m", "venv", env], capture_output=True, text=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if out.returncode != 0:
                raise RuntimeError(out.stderr or out.stdout)
            log_cb("Создано: " + env)

        steps = [call("Создание venv " + env, make_venv),
                 proc("pip install -U pip", py, ["-m", "pip", "install", "-U", "pip"])]
        if kind == "asr":
            steps.append(proc("pip install -r requirements-asr.txt", py, ["-m", "pip", "install", "-r", REQ_ASR]))
        else:
            steps.append(proc("pip install torch torchaudio (cu126)", py,
                              ["-m", "pip", "install", "torch", "torchaudio", "--index-url", TORCH_INDEX]))
            steps.append(proc("pip install -r requirements-tts.txt", py, ["-m", "pip", "install", "-r", REQ_TTS]))
        key = "asr_python" if kind == "asr" else "tts_python"
        return Fix("Создать окружение", steps, after=lambda: self._save(key, py),
                   confirm=("Будет создан %s и скачаны пакеты%s. Продолжить?" %
                            (env, " (~5 ГБ, torch CUDA)" if kind == "tts" else " (~1 ГБ)")))


def log_exc(prefix):
    log.warning(prefix, exc_info=True)
    return sys.exc_info()[1]
