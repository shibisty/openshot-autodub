#!/usr/bin/env python3
"""
Local CosyVoice TTS server for the OpenShot Dubbing panel.

Runs in a Python that has the CosyVoice dependencies (e.g. C:\\ComfyUI\\venv),
imports the vendored `cosyvoice` / `matcha` packages straight from the
ComfyUI_FL-CosyVoice3 folder and uses the already downloaded models. ComfyUI
itself is NOT started or required.

The model is loaded once; jobs are read from stdin as JSON lines:

  {"id": "s0001", "text": "Привет", "ref": "ref.wav", "ref_text": "Hello",
   "ref_lang": "en", "lang": "ru", "speed": 1.0, "seed": 42,
   "out": "dubs/s0001.wav", "slot": 2.1, "fit_speed": true, "mode": "auto"}
  {"cmd": "quit"}

and answered on stdout (non-"@@" lines are library noise):

  @@{"type": "ready", "model": "...", "sample_rate": 24000, "device": "cuda"}
  @@{"type": "started", "id": "s0001"}
  @@{"type": "done", "id": "s0001", "path": "...", "duration": 1.9, "speed": 1.0, "mode": "cross_lingual"}
  @@{"type": "failed", "id": "s0001", "message": "..."}
  @@{"type": "error", "message": "..."}      (fatal, e.g. model load failed)

Usage:
  python cosyvoice_tts.py --cosyvoice-src C:\\ComfyUI\\custom_nodes\\ComfyUI_FL-CosyVoice3
                          --models-dir C:\\ComfyUI\\models\\cosyvoice --model Fun-CosyVoice3-0.5B
  python cosyvoice_tts.py ... --once '{"id": ..., ...}'     (single job, for testing)
"""

import argparse
import json
import os
import random
import sys
import time
import traceback

V3_PREFIX = "You are a helpful assistant.<|endofprompt|>"
V1_LANG_TAGS = {"en": "<|en|>", "zh": "<|zh|>", "ja": "<|jp|>", "ko": "<|ko|>", "de": "<|de|>",
                "es": "<|es|>", "fr": "<|fr|>", "it": "<|it|>", "ru": "<|ru|>"}

_real_stdout = sys.stdout


def emit(obj):
    _real_stdout.write("@@" + json.dumps(obj, ensure_ascii=False) + "\n")
    _real_stdout.flush()


def find_model_dir(models_dir, model_name):
    """Locate the folder with cosyvoice*.yaml (handles HF cache 'snapshots/<hash>' layout)."""
    candidates = []
    base = os.path.join(models_dir, model_name) if model_name else models_dir
    if not os.path.isdir(base):
        base = models_dir
    for root, _dirs, files in os.walk(base):
        for cfg in ("cosyvoice3.yaml", "cosyvoice2.yaml", "cosyvoice.yaml"):
            if cfg in files:
                candidates.append(root)
                break
    if not candidates:
        raise FileNotFoundError("cosyvoice*.yaml not found under %s" % base)
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


class Engine:
    def __init__(self, args):
        src = os.path.abspath(args.cosyvoice_src)
        if not os.path.isdir(os.path.join(src, "cosyvoice")):
            raise FileNotFoundError("Нет пакета cosyvoice в %s" % src)
        for path in (src, os.path.join(src, "cosyvoice"), os.path.join(src, "matcha")):
            if path not in sys.path:
                sys.path.insert(0, path)
        # The vendored package ships its 3rd-party Matcha-TTS under the node root.
        import torch
        self.torch = torch
        from cosyvoice.cli.cosyvoice import AutoModel
        self.model_dir = find_model_dir(args.models_dir, args.model)
        self.model = AutoModel(model_dir=self.model_dir, load_trt=False, fp16=bool(args.fp16))
        self.sample_rate = int(self.model.sample_rate)
        self.is_v3 = os.path.isfile(os.path.join(self.model_dir, "cosyvoice3.yaml"))
        self.is_v1 = os.path.isfile(os.path.join(self.model_dir, "cosyvoice.yaml"))
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

    # ------------------------------------------------------------------
    def _seed(self, seed):
        seed = int(seed if seed is not None else 42)
        if seed >= 0:
            self.torch.manual_seed(seed)
            random.seed(seed)
            if self.torch.cuda.is_available():
                self.torch.cuda.manual_seed_all(seed)

    def _mode(self, job):
        mode = job.get("mode") or "auto"
        if mode != "auto":
            return mode
        same_lang = job.get("ref_lang") and job.get("ref_lang") == job.get("lang")
        return "zero_shot" if same_lang and (job.get("ref_text") or "").strip() else "cross_lingual"

    def synth(self, job, speed):
        self._seed(job.get("seed", 42))
        text = (job.get("text") or "").strip()
        if not text:
            raise ValueError("пустой текст")
        mode = self._mode(job)
        ref = job["ref"]
        frontend = bool(job.get("text_frontend", True))
        if mode == "zero_shot":
            prompt_text = job.get("ref_text", "").strip()
            if self.is_v3:
                prompt_text = V3_PREFIX + prompt_text
            gen = self.model.inference_zero_shot(tts_text=text, prompt_text=prompt_text, prompt_wav=ref,
                                                 stream=False, speed=speed, text_frontend=frontend)
        elif mode == "instruct" and hasattr(self.model, "inference_instruct2"):
            instruct = (job.get("instruct") or "").strip()
            instruct = (V3_PREFIX.split("<|endofprompt|>")[0] + " " + instruct + "<|endofprompt|>") \
                if self.is_v3 else instruct
            gen = self.model.inference_instruct2(tts_text=text, instruct_text=instruct, prompt_wav=ref,
                                                 stream=False, speed=speed, text_frontend=frontend)
        else:
            mode = "cross_lingual"
            if self.is_v3:
                tts_text = V3_PREFIX + text
            elif self.is_v1:
                tts_text = V1_LANG_TAGS.get(job.get("lang", ""), "") + text
            else:
                tts_text = text
            gen = self.model.inference_cross_lingual(tts_text=tts_text, prompt_wav=ref, stream=False,
                                                     speed=speed, text_frontend=frontend)
        chunks = [out["tts_speech"] for out in gen]
        if not chunks:
            raise RuntimeError("модель не вернула аудио")
        wave = self.torch.cat(chunks, dim=-1).detach().cpu().float().numpy().reshape(-1)
        return wave, mode

    def run_job(self, job):
        import numpy as np
        import soundfile as sf
        speed = max(0.5, min(2.0, float(job.get("speed") or 1.0)))
        wave, mode = self.synth(job, speed)
        duration = len(wave) / float(self.sample_rate)
        slot = float(job.get("slot") or 0.0)
        if job.get("fit_speed") and slot > 0.2 and duration > slot * 1.06:
            new_speed = min(float(job.get("max_speed") or 1.8), speed * duration / slot * 1.02)
            if new_speed > speed + 0.03:
                emit({"type": "log", "id": job["id"],
                      "message": "%.2fs > %.2fs, повтор со скоростью %.2f" % (duration, slot, new_speed)})
                wave, mode = self.synth(job, new_speed)
                speed = new_speed
                duration = len(wave) / float(self.sample_rate)
        # trim leading/trailing near-silence so the phrase starts on its timecode
        wave = trim_silence(wave, self.sample_rate)
        duration = len(wave) / float(self.sample_rate)
        peak = float(np.max(np.abs(wave))) if len(wave) else 0.0
        if peak > 0.99:
            wave = wave * (0.97 / peak)
        out = job["out"]
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        sf.write(out, wave, self.sample_rate, subtype="PCM_16")
        return {"type": "done", "id": job["id"], "path": out, "duration": round(duration, 3),
                "speed": round(speed, 3), "mode": mode}


def trim_silence(wave, sr, threshold=0.01, keep=0.04):
    import numpy as np
    if len(wave) == 0:
        return wave
    idx = np.where(np.abs(wave) > threshold)[0]
    if len(idx) == 0:
        return wave
    pad = int(keep * sr)
    return wave[max(0, idx[0] - pad): min(len(wave), idx[-1] + pad)]


REQUIRED_MODULES = ["torch", "torchaudio", "numpy", "soundfile", "onnxruntime", "hyperpyyaml", "modelscope",
                    "transformers", "librosa", "inflect", "omegaconf", "conformer", "diffusers", "x_transformers",
                    "wetext", "whisper", "einops", "regex", "tiktoken", "pyworld", "huggingface_hub"]


def cmd_check(args):
    import importlib
    modules = {}
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
            modules[name] = True
        except Exception:  # noqa: BLE001
            modules[name] = False
    result = {"type": "result", "python": sys.executable, "version": sys.version.split()[0],
              "modules": modules}
    if modules.get("torch"):
        import torch
        result["torch"] = torch.__version__
        result["cuda"] = bool(torch.cuda.is_available())
        if result["cuda"]:
            result["gpu"] = torch.cuda.get_device_name(0)
    src = os.path.abspath(args.cosyvoice_src) if args.cosyvoice_src else ""
    result["cosyvoice_src_ok"] = bool(src) and os.path.isdir(os.path.join(src, "cosyvoice")) \
        and os.path.isdir(os.path.join(src, "matcha"))
    if result["cosyvoice_src_ok"] and modules.get("torch"):
        try:
            if src not in sys.path:
                sys.path.insert(0, src)
            import cosyvoice.cli.cosyvoice  # noqa: F401
            result["cosyvoice_import"] = True
        except Exception as exc:  # noqa: BLE001
            result["cosyvoice_import"] = False
            result["cosyvoice_error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        result["model_dir"] = find_model_dir(args.models_dir, args.model)
    except Exception:  # noqa: BLE001
        result["model_dir"] = ""
    emit(result)
    return 0


def cmd_download(args):
    from huggingface_hub import snapshot_download
    target = os.path.join(args.models_dir, args.model)
    os.makedirs(target, exist_ok=True)
    emit({"type": "progress", "value": 0.05, "message": "Скачивание %s → %s" % (args.download_model, target)})
    path = snapshot_download(repo_id=args.download_model, cache_dir=target)
    emit({"type": "progress", "value": 1.0, "message": "Модель скачана"})
    emit({"type": "result", "path": path})
    return 0


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--cosyvoice-src", default="")
    p.add_argument("--models-dir", required=True)
    p.add_argument("--check", action="store_true", help="report environment, do not load the model")
    p.add_argument("--download-model", default="", metavar="HF_REPO",
                   help="download a model repo (e.g. FunAudioLLM/Fun-CosyVoice3-0.5B-2512) into models-dir/<model>")
    p.add_argument("--model", default="Fun-CosyVoice3-0.5B")
    p.add_argument("--fp16", action="store_true")
    p.add_argument("--once", default="")
    args = p.parse_args(argv)

    # Library prints must not corrupt the protocol: route print() to stderr.
    sys.stdout = sys.stderr
    if args.check:
        return cmd_check(args)
    if args.download_model:
        return cmd_download(args)
    try:
        t0 = time.time()
        engine = Engine(args)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        emit({"type": "error", "message": "Не удалось загрузить CosyVoice: %s: %s" % (type(exc).__name__, exc)})
        return 2
    emit({"type": "ready", "model": engine.model_dir, "sample_rate": engine.sample_rate,
          "device": engine.device, "load_seconds": round(time.time() - t0, 1)})

    lines = [args.once] if args.once else iter(sys.stdin.readline, "")
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            job = json.loads(line)
        except ValueError:
            emit({"type": "error", "message": "bad json"})
            continue
        if job.get("cmd") == "quit":
            break
        emit({"type": "started", "id": job.get("id")})
        try:
            emit(engine.run_job(job))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            emit({"type": "failed", "id": job.get("id"), "message": "%s: %s" % (type(exc).__name__, exc)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
