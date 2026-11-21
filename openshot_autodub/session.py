"""
 @file
 @brief Dubbing session data model (one per source video), persisted as JSON
        in a working folder next to the project.

 The session is deliberately kept OUT of the .osp project data so that
 OpenShot's undo/redo and project schema are untouched. Everything lives in:

     <project dir>/<project name>_dubbing/<video name>/session.json
     (or ~/.openshot_qt/dubbing/<video name>-<hash>/ for unsaved projects)
"""

import hashlib
import json
import os
import re
import time
import uuid
import wave

from classes import info
from classes.app import get_app
from classes.logger import log

# Languages CosyVoice3 can speak (CrossLingual node choices).
COSYVOICE_LANGUAGES = [
    ("ru", "Русский"), ("en", "English"), ("es", "Español"), ("de", "Deutsch"),
    ("fr", "Français"), ("it", "Italiano"), ("ja", "日本語"), ("ko", "한국어"), ("zh", "中文"),
]

STATUS_NEW = ""
STATUS_TRANSLATED = "translated"
STATUS_DUBBING = "dubbing"
STATUS_DUBBED = "dubbed"
STATUS_ERROR = "error"
STATUS_STALE = "stale"  # text changed after dub was generated

DEFAULT_MIX = {
    "overlay": True,          # play dub over original audio (else original is muted)
    "original_volume": 25,    # % of original while overlaid
    "dub_volume": 100,        # % of dub clips
    "duck_only_speech": True, # lower original only while a dub phrase plays
    "duck_fade": 0.15,        # seconds of ramp around each phrase
    "fit_speed": True,        # re-generate faster if dub is longer than its slot
    "track_per_speaker": True,
}


def format_time(seconds):
    seconds = max(0.0, float(seconds or 0.0))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    if h:
        return "%d:%02d:%06.3f" % (h, m, s)
    return "%d:%06.3f" % (m, s)


def parse_time(text):
    """Accept 12.5 | 0:12.5 | 1:02:03.250 (comma decimal ok)."""
    text = str(text or "").strip().replace(",", ".")
    if not text:
        raise ValueError("empty time")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError("bad time: %s" % text)
    total = 0.0
    for part in parts:
        total = total * 60.0 + float(part)
    if total < 0:
        raise ValueError("negative time")
    return total


def _safe_name(name):
    name = re.sub(r"[^\w\-. ]+", "_", str(name or "video"), flags=re.UNICODE).strip(" .")
    return name[:60] or "video"


def session_dir_for(video_path):
    app = get_app()
    project_path = getattr(app.project, "current_filepath", None)
    stem = _safe_name(os.path.splitext(os.path.basename(video_path))[0])
    if project_path:
        base = os.path.join(
            os.path.dirname(project_path),
            _safe_name(os.path.splitext(os.path.basename(project_path))[0]) + "_dubbing")
        return os.path.join(base, stem)
    digest = hashlib.sha1(os.path.abspath(video_path).encode("utf-8")).hexdigest()[:8]
    return os.path.join(info.USER_PATH, "dubbing", "%s-%s" % (stem, digest))


class Segment(dict):
    """A sentence. dict subclass so it serialises as-is."""

    @staticmethod
    def new(start, end, text, speaker="SPEAKER_A", seg_id=None):
        return Segment({
            "id": seg_id or "u" + uuid.uuid4().hex[:8],
            "start": float(start),
            "end": float(end),
            "speaker": speaker,
            "text": text,
            "translation": "",
            "status": STATUS_NEW,
            "dub_path": "",
            "dub_duration": 0.0,
            "dub_speed": 1.0,
            "dub_text": "",
            "error": "",
            "clip_id": "",
        })

    @property
    def duration(self):
        return max(0.0, float(self["end"]) - float(self["start"]))


class DubbingSession:
    VERSION = 1

    def __init__(self, video_path, file_id=None):
        self.video_path = os.path.abspath(video_path)
        self.file_id = file_id
        self.dir = session_dir_for(video_path)
        self.path = os.path.join(self.dir, "session.json")
        self.source_language = "auto"
        self.detected_language = ""
        self.target_language = "ru"
        self.duration = 0.0
        self.audio16k = ""
        self.diarization = ""
        self.segments = []
        self.speaker_refs = {}     # speaker -> wav path override chosen by user
        self.mix = dict(DEFAULT_MIX)
        self.placed_clip_ids = []
        self.placed_track_numbers = []
        self.original_clip_id = ""
        self.original_volume_backup = None

    # ------------------------------------------------------------ persist --
    @classmethod
    def load_or_create(cls, video_path, file_id=None):
        session = cls(video_path, file_id)
        if os.path.isfile(session.path):
            try:
                with open(session.path, "r", encoding="utf-8") as fh:
                    session._from_dict(json.load(fh))
                session.file_id = file_id or session.file_id
            except Exception:
                log.warning("Dubbing: unable to read %s", session.path, exc_info=True)
        return session

    def _from_dict(self, data):
        self.source_language = data.get("source_language", "auto")
        self.detected_language = data.get("detected_language", "")
        self.target_language = data.get("target_language", "ru")
        self.duration = float(data.get("duration", 0.0) or 0.0)
        self.audio16k = data.get("audio16k", "")
        self.diarization = data.get("diarization", "")
        self.speaker_refs = dict(data.get("speaker_refs") or {})
        self.mix = dict(DEFAULT_MIX)
        self.mix.update(data.get("mix") or {})
        self.placed_clip_ids = list(data.get("placed_clip_ids") or [])
        self.placed_track_numbers = list(data.get("placed_track_numbers") or [])
        self.original_clip_id = data.get("original_clip_id", "")
        self.original_volume_backup = data.get("original_volume_backup")
        self.file_id = data.get("file_id") or self.file_id
        self.segments = []
        for raw in data.get("segments") or []:
            seg = Segment.new(raw.get("start", 0), raw.get("end", 0), raw.get("text", ""))
            seg.update(raw)
            self.segments.append(seg)

    def to_dict(self):
        return {
            "version": self.VERSION,
            "video_path": self.video_path,
            "file_id": self.file_id,
            "source_language": self.source_language,
            "detected_language": self.detected_language,
            "target_language": self.target_language,
            "duration": self.duration,
            "audio16k": self.audio16k,
            "diarization": self.diarization,
            "speaker_refs": self.speaker_refs,
            "mix": self.mix,
            "placed_clip_ids": self.placed_clip_ids,
            "placed_track_numbers": self.placed_track_numbers,
            "original_clip_id": self.original_clip_id,
            "original_volume_backup": self.original_volume_backup,
            "segments": self.segments,
            "saved_at": time.time(),
        }

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ editing --
    def apply_transcript(self, result):
        self.detected_language = result.get("language", "") or ""
        self.duration = float(result.get("duration", 0.0) or 0.0)
        self.audio16k = result.get("audio16k", "") or self.audio16k
        self.diarization = result.get("diarization", "")
        self.segments = []
        for raw in result.get("segments") or []:
            self.segments.append(Segment.new(
                raw["start"], raw["end"], raw.get("text", ""),
                raw.get("speaker", "SPEAKER_A"), raw.get("id")))
        self.placed_clip_ids = []

    def segment(self, seg_id):
        for seg in self.segments:
            if seg["id"] == seg_id:
                return seg
        return None

    def index_of(self, seg_id):
        for i, seg in enumerate(self.segments):
            if seg["id"] == seg_id:
                return i
        return -1

    def remove(self, seg_id):
        self.segments = [s for s in self.segments if s["id"] != seg_id]

    def sort(self):
        self.segments.sort(key=lambda s: (float(s["start"]), float(s["end"])))

    def speakers(self):
        seen = []
        for seg in self.segments:
            if seg["speaker"] not in seen:
                seen.append(seg["speaker"])
        return seen

    def rename_speaker(self, old, new):
        for seg in self.segments:
            if seg["speaker"] == old:
                seg["speaker"] = new
        if old in self.speaker_refs:
            self.speaker_refs[new] = self.speaker_refs.pop(old)

    def effective_source_language(self):
        if self.source_language and self.source_language != "auto":
            return self.source_language
        return self.detected_language or "en"

    def slot_for(self, seg):
        """Seconds the dub may occupy: up to the next phrase start (but never less than the phrase)."""
        idx = self.index_of(seg["id"])
        own = seg.duration
        nxt = None
        for other in self.segments[idx + 1:]:
            if float(other["start"]) >= float(seg["start"]):
                nxt = other
                break
        if nxt is None:
            return own + 0.8
        gap_end = float(nxt["start"]) - float(seg["start"]) - 0.05
        return max(own, min(gap_end, own + 1.5))

    # -------------------------------------------------- reference audio --
    def ref_dir(self):
        path = os.path.join(self.dir, "refs")
        os.makedirs(path, exist_ok=True)
        return path

    def dub_dir(self):
        path = os.path.join(self.dir, "dubs")
        os.makedirs(path, exist_ok=True)
        return path

    def reference_for(self, seg, min_seconds=3.0, max_seconds=12.0):
        """
        Build the CosyVoice prompt for a phrase -> (wav_path, transcript_of_wav).

        The phrase's own original audio is used when long enough (it carries the
        intonation of exactly that line). Short phrases borrow neighbouring lines
        of the same speaker until ~min_seconds is reached. A per-speaker override
        chosen by the user wins.
        """
        override = self.speaker_refs.get(seg["speaker"])
        if isinstance(override, str):
            override = {"path": override, "text": ""}
        if override and os.path.isfile(override.get("path", "")):
            return override["path"], override.get("text", "")
        if not self.audio16k or not os.path.isfile(self.audio16k):
            raise RuntimeError("Нет audio16k.wav – сначала выполните распознавание")

        chosen = [seg]
        total = seg.duration
        if total < min_seconds:
            same = [s for s in self.segments if s["speaker"] == seg["speaker"] and s["id"] != seg["id"]]
            same.sort(key=lambda s: abs(float(s["start"]) - float(seg["start"])))
            for other in same:
                if total >= min_seconds or total + other.duration > max_seconds:
                    continue
                chosen.append(other)
                total += other.duration
            chosen.sort(key=lambda s: float(s["start"]))
        out = os.path.join(self.ref_dir(), "%s.wav" % seg["id"])
        write_wav_slices(self.audio16k, [(float(s["start"]), float(s["end"])) for s in chosen], out,
                         max_seconds=max_seconds)
        text = " ".join((s.get("text") or "").strip() for s in chosen).strip()
        return out, text


def write_wav_slices(src_wav, pieces, dst_wav, pad=0.08, gap=0.25, max_seconds=12.0):
    """Concatenate [(start, end), ...] seconds from a PCM wav, with short silences in between."""
    with wave.open(src_wav, "rb") as src:
        sr = src.getframerate()
        width = src.getsampwidth()
        channels = src.getnchannels()
        nframes = src.getnframes()
        silence = b"\x00" * int(gap * sr) * width * channels
        chunks = []
        written = 0.0
        for start, end in pieces:
            a = max(0, int((start - pad) * sr))
            b = min(nframes, int((end + pad) * sr))
            if b <= a:
                continue
            room = max_seconds - written
            if room <= 0.3:
                break
            b = min(b, a + int(room * sr))
            src.setpos(a)
            chunks.append(src.readframes(b - a))
            chunks.append(silence)
            written += (b - a) / float(sr) + gap
    if not chunks:
        raise RuntimeError("Пустой фрагмент для образца голоса")
    os.makedirs(os.path.dirname(dst_wav), exist_ok=True)
    with wave.open(dst_wav, "wb") as dst:
        dst.setnchannels(channels)
        dst.setsampwidth(width)
        dst.setframerate(sr)
        dst.writeframes(b"".join(chunks[:-1]))
    return dst_wav
