"""
 @file
 @brief Generates dub phrases with FL CosyVoice3 (Cross-Lingual) through the
        ComfyUI HTTP API that OpenShot already uses (classes.comfy_client).

 Graph sent per phrase (API format):
     LoadAudio(ref.wav) ─┐
     ModelLoader ────────┼─> FL_CosyVoice3_CrossLingual(text, speed, lang) ─> SaveAudio
"""

import os
import time
import uuid

from qt_api import QThread, pyqtSignal

from classes.comfy_client import ComfyClient
from classes.logger import log
from .config import setting

POLL_SECONDS = 0.5
TIMEOUT_SECONDS = 15 * 60


def comfy_url():
    return str(setting("comfy_url", "http://127.0.0.1:8188")).rstrip("/")


def build_graph(ref_path, text, language, speed, seed, prefix):
    return {
        "1": {"class_type": "LoadAudio", "inputs": {"audio": ref_path}},
        "2": {
            "class_type": "FL_CosyVoice3_ModelLoader",
            "inputs": {
                "model_version": setting("cosyvoice_model", "Fun-CosyVoice3-0.5B"),
                "download_source": "HuggingFace",
                "device": "auto",
                "force_redownload": False,
                "force_reload": False,
            },
        },
        "3": {
            "class_type": "FL_CosyVoice3_CrossLingual",
            "inputs": {
                "model": ["2", 0],
                "reference_audio": ["1", 0],
                "text": text,
                "speed": round(max(0.5, min(2.0, float(speed))), 3),
                "target_language": language if language else "auto",
                "seed": int(seed),
                "text_frontend": True,
            },
        },
        "4": {"class_type": "SaveAudio", "inputs": {"audio": ["3", 0], "filename_prefix": prefix}},
    }


def media_duration(path):
    """Duration in seconds using libopenshot (works for flac/wav/mp3)."""
    try:
        import openshot
        reader = openshot.FFmpegReader(path)
        reader.Open()
        duration = float(reader.info.duration)
        reader.Close()
        return duration
    except Exception:
        log.warning("Dubbing: cannot read duration of %s", path, exc_info=True)
        return 0.0


class DubJob(dict):
    """seg_id, text, ref_path, language, slot, fit_speed, out_dir"""


class ComfyDubber(QThread):
    segment_started = pyqtSignal(str)
    segment_done = pyqtSignal(str, str, float, float)   # seg_id, path, duration, speed
    segment_failed = pyqtSignal(str, str)
    queue_progress = pyqtSignal(int, int)                # done, total

    def __init__(self, jobs, parent=None):
        super().__init__(parent)
        self.jobs = list(jobs)
        self._cancel = False
        self.client = ComfyClient(comfy_url())
        self.client_id = "openshot-dub-" + uuid.uuid4().hex[:8]
        self._current_prompt = None

    def cancel(self):
        self._cancel = True
        if self._current_prompt:
            try:
                self.client.interrupt(self._current_prompt)
            except Exception:
                pass

    def run(self):
        try:
            self.client.ping(timeout=2.0)
        except Exception as exc:
            for job in self.jobs:
                self.segment_failed.emit(job["seg_id"], "ComfyUI недоступен (%s): %s" % (comfy_url(), exc))
            return
        total = len(self.jobs)
        for n, job in enumerate(self.jobs):
            if self._cancel:
                break
            self.segment_started.emit(job["seg_id"])
            try:
                path, duration, speed = self._dub(job)
                self.segment_done.emit(job["seg_id"], path, duration, speed)
            except Exception as exc:
                log.warning("Dubbing failed for %s", job.get("seg_id"), exc_info=True)
                self.segment_failed.emit(job["seg_id"], str(exc))
            self.queue_progress.emit(n + 1, total)

    def _dub(self, job):
        speed = float(job.get("speed") or 1.0)
        path, duration = self._generate(job, speed, attempt=0)
        slot = float(job.get("slot") or 0.0)
        if job.get("fit_speed") and slot > 0.2 and duration > slot * 1.06 and not self._cancel:
            new_speed = min(1.8, max(speed, speed * duration / slot * 1.02))
            if new_speed > speed + 0.03:
                log.info("Dubbing %s: %.2fs > slot %.2fs, retry at speed %.2f",
                         job["seg_id"], duration, slot, new_speed)
                path, duration = self._generate(job, new_speed, attempt=1)
                speed = new_speed
        return path, duration, speed

    def _generate(self, job, speed, attempt):
        prefix = "openshot_dub/%s_%s" % (job["seg_id"], uuid.uuid4().hex[:6])
        graph = build_graph(job["ref_path"], job["text"], job["language"], speed,
                            int(setting("seed", 42) or 42), prefix)
        prompt_id = self.client.queue_prompt(graph, self.client_id)
        self._current_prompt = prompt_id
        started = time.time()
        entry = None
        while not self._cancel:
            if time.time() - started > TIMEOUT_SECONDS:
                raise RuntimeError("Таймаут ComfyUI")
            hist = self.client.history(prompt_id) or {}
            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise RuntimeError(self._status_error(status))
                if status.get("completed") or entry.get("outputs"):
                    break
            time.sleep(POLL_SECONDS)
        self._current_prompt = None
        if self._cancel:
            raise RuntimeError("Отменено")
        refs = [r for r in ComfyClient.extract_file_outputs(entry, save_node_ids=["4"]) if r.get("filename")]
        if not refs:
            raise RuntimeError("ComfyUI не вернул аудио (проверьте узлы FL CosyVoice3 и SaveAudio)")
        ref = refs[0]
        ext = os.path.splitext(ref["filename"])[1] or ".flac"
        dest = os.path.join(job["out_dir"], "%s_v%d%s" % (job["seg_id"], int(time.time()) % 100000 + attempt, ext))
        self.client.download_output_file(ref, dest)
        return dest, media_duration(dest)

    @staticmethod
    def _status_error(status):
        for kind, data in status.get("messages") or []:
            if kind == "execution_error" and isinstance(data, dict):
                return "%s: %s" % (data.get("node_type", ""), ComfyClient.summarize_error_text(
                    data.get("exception_message", "")))
        return "Ошибка выполнения в ComfyUI"
