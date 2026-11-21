"""
AutoDub configuration: ~/.openshot_qt/autodub.json (independent of OpenShot's own settings).

Missing values are auto-detected (existing ComfyUI / venvs) or fall back to
folders inside the plugin directory (envs/, vendor/, models/).
"""

import json
import os

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKERS_DIR = os.path.join(PLUGIN_ROOT, "workers")
USER_PATH = os.path.join(os.path.expanduser("~"), ".openshot_qt")
CONFIG_PATH = os.path.join(USER_PATH, "autodub.json")

LOCAL_ENVS = os.path.join(PLUGIN_ROOT, "envs")
LOCAL_ASR_ENV = os.path.join(LOCAL_ENVS, "asr")
LOCAL_TTS_ENV = os.path.join(LOCAL_ENVS, "tts")
LOCAL_VENDOR = os.path.join(PLUGIN_ROOT, "vendor", "ComfyUI_FL-CosyVoice3")
LOCAL_MODELS = os.path.join(PLUGIN_ROOT, "models", "cosyvoice")

COSYVOICE_REPO_ZIP = "https://codeload.github.com/filliptm/ComfyUI_FL-CosyVoice3/zip/refs/heads/main"
COSYVOICE_MODELS = {
    "Fun-CosyVoice3-0.5B": "FunAudioLLM/Fun-CosyVoice3-0.5B-2512",
    "CosyVoice2-0.5B": "FunAudioLLM/CosyVoice2-0.5B",
}
WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo", "distil-large-v3"]


def venv_python(env_dir):
    for rel in (("Scripts", "python.exe"), ("bin", "python")):
        path = os.path.join(env_dir, *rel)
        if os.path.isfile(path):
            return path
    return os.path.join(env_dir, "Scripts", "python.exe") if os.name == "nt" else os.path.join(env_dir, "bin", "python")


def _comfy_roots():
    home = os.path.expanduser("~")
    roots = [r"C:\ComfyUI", os.path.join(home, "ComfyUI"), r"C:\ComfyUI_windows_portable\ComfyUI",
             os.path.join(home, "ComfyUI_windows_portable", "ComfyUI"), r"D:\ComfyUI"]
    return [r for r in roots if os.path.isdir(r)]


def _comfy_python(root):
    for cand in (os.path.join(root, "venv", "Scripts", "python.exe"),
                 os.path.join(root, ".venv", "Scripts", "python.exe"),
                 os.path.join(os.path.dirname(root), "python_embeded", "python.exe")):
        if os.path.isfile(cand):
            return cand
    return ""


def detect_defaults():
    d = {
        "asr_python": venv_python(LOCAL_ASR_ENV),
        "whisper_model": "medium",
        "whisper_device": "cuda",
        "whisper_compute": "int8",
        "hf_token": "",
        "tts_engine": "local",            # local | comfyui
        "tts_python": venv_python(LOCAL_TTS_ENV),
        "cosyvoice_src": LOCAL_VENDOR,
        "cosyvoice_models_dir": LOCAL_MODELS,
        "cosyvoice_model": "Fun-CosyVoice3-0.5B",
        "tts_mode": "auto",               # auto | cross_lingual | zero_shot
        "tts_fp16": False,
        "seed": 42,
        "comfy_url": "http://127.0.0.1:8188",
    }
    for root in _comfy_roots():
        node = os.path.join(root, "custom_nodes", "ComfyUI_FL-CosyVoice3")
        if os.path.isdir(os.path.join(node, "cosyvoice")):
            d["cosyvoice_src"] = node
            py = _comfy_python(root)
            if py:
                d["tts_python"] = py
        models = os.path.join(root, "models", "cosyvoice")
        if os.path.isdir(models):
            d["cosyvoice_models_dir"] = models
        break
    return d


class Config(dict):
    def __init__(self):
        super().__init__(detect_defaults())
        self._stored = {}
        self.load()

    def load(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                self._stored = json.load(fh) or {}
        except (OSError, ValueError):
            self._stored = {}
        self.update(self._stored)

    def set(self, key, value):
        self[key] = value
        self._stored[key] = value

    def save(self):
        os.makedirs(USER_PATH, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._stored, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)


_CONFIG = None


def config():
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config()
    return _CONFIG


def setting(key, fallback=None):
    """Plugin setting; `comfy-ui-url` falls back to OpenShot's own preference."""
    value = config().get(key)
    if key == "comfy_url":
        try:
            from classes.app import get_app
            value = get_app().get_settings().get("comfy_url") or value
        except Exception:
            pass
    return fallback if value in (None, "") else value


def campplus_path():
    models_dir = setting("cosyvoice_models_dir", LOCAL_MODELS)
    model = setting("cosyvoice_model", "Fun-CosyVoice3-0.5B")
    for base in (os.path.join(models_dir, model), models_dir):
        if os.path.isdir(base):
            for root, _dirs, files in os.walk(base):
                if "campplus.onnx" in files:
                    return os.path.join(root, "campplus.onnx")
    return ""


def cosyvoice_model_dir():
    """Folder holding cosyvoice*.yaml for the selected model, or ''."""
    models_dir = setting("cosyvoice_models_dir", LOCAL_MODELS)
    base = os.path.join(models_dir, setting("cosyvoice_model", "Fun-CosyVoice3-0.5B"))
    if not os.path.isdir(base):
        return ""
    for root, _dirs, files in os.walk(base):
        if any(f in files for f in ("cosyvoice3.yaml", "cosyvoice2.yaml", "cosyvoice.yaml")):
            if any(f in files for f in ("llm.pt", "llm.rl.pt")) and "flow.pt" in files:
                return root
    return ""
