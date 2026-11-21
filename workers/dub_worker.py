#!/usr/bin/env python3
"""
OpenShot Dubbing worker.

Runs in a SEPARATE Python environment (e.g. translator_electron/backend/venv)
that has the heavy ML dependencies (faster-whisper, argostranslate, numpy,
soundfile, onnxruntime). OpenShot itself never imports this module; it launches
it through QProcess and reads JSON lines from stdout:

    @@{"type": "progress", "value": 0.42, "message": "..."}
    @@{"type": "result", ...}
    @@{"type": "error", "message": "..."}

Commands:
    transcribe  --video V --workdir W [--model medium] [--device cuda]
                [--compute int8] [--language auto] [--speakers 0]
                [--campplus path/to/campplus.onnx]
        -> writes W/audio16k.wav and W/transcript.json, prints result

    translate   --request req.json
        req.json = {"source": "en", "target": "ru", "items": [{"id": "..", "text": ".."}]}
        -> prints result {"items": [{"id": "..", "text": ".."}]}

    check [--whisper-model medium]
        -> modules, CUDA devices, whisper model cached?, installed Argos pairs

    download-whisper --model medium        (HuggingFace cache)
    install-argos --source en --target ru  (via English pivot if needed)
"""

import argparse
import json
import math
import os
import re
import sys
import traceback

# ---------------------------------------------------------------- protocol --

def emit(obj):
    sys.stdout.write("@@" + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def progress(value, message=""):
    emit({"type": "progress", "value": round(float(value), 4), "message": message})


def log(message):
    sys.stderr.write(str(message) + "\n")
    sys.stderr.flush()


# ------------------------------------------------------------ CUDA helpers --

def register_nvidia_dll_dirs():
    """Windows: make cublas/cudnn from nvidia-* pip packages visible to CTranslate2."""
    if sys.platform != "win32":
        return
    for pkg_name in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc"):
        try:
            module = __import__(pkg_name, fromlist=["_"])
        except ImportError:
            continue
        pkg_dir = None
        if getattr(module, "__file__", None):
            pkg_dir = os.path.dirname(module.__file__)
        elif getattr(module, "__path__", None):
            paths = list(module.__path__)
            pkg_dir = paths[0] if paths else None
        if not pkg_dir:
            continue
        for sub in ("bin", "lib"):
            cand = os.path.join(pkg_dir, sub)
            if os.path.isdir(cand):
                try:
                    os.add_dll_directory(cand)
                except (OSError, AttributeError):
                    pass
                if cand not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = cand + os.pathsep + os.environ.get("PATH", "")


# ------------------------------------------------------ sentence splitting --

SENTENCE_END_RE = re.compile(r"[.!?…。！？]+[\"'»”)\]]*$")
SOFT_BREAK_RE = re.compile(r"[,;:—–]+[\"'»”)\]]*$")


def split_sentences(words, max_gap=1.0, max_len=14.0, soft_len=7.0):
    """
    Group whisper words [{"start","end","word"}] into sentences.

    A sentence ends on terminal punctuation, on a pause longer than max_gap,
    or, if it has grown past soft_len seconds, on a comma-like break, and is
    force-cut at max_len seconds.
    """
    sentences = []
    cur = []

    def flush():
        if not cur:
            return
        text = "".join(w["word"] for w in cur).strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            sentences.append({
                "start": round(float(cur[0]["start"]), 3),
                "end": round(float(cur[-1]["end"]), 3),
                "text": text,
            })
        cur.clear()

    for i, w in enumerate(words):
        token = w.get("word", "")
        if not token.strip():
            continue
        if cur:
            gap = float(w["start"]) - float(cur[-1]["end"])
            if gap > max_gap:
                flush()
        cur.append(w)
        dur = float(cur[-1]["end"]) - float(cur[0]["start"])
        stripped = token.strip()
        if SENTENCE_END_RE.search(stripped) and not _is_abbreviation(stripped):
            flush()
        elif dur >= max_len:
            flush()
        elif dur >= soft_len and SOFT_BREAK_RE.search(stripped):
            flush()
    flush()
    return sentences


_ABBREV = {"mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "etc.", "e.g.", "i.e.", "jr.", "sr.", "prof."}


def _is_abbreviation(token):
    return token.lower() in _ABBREV


# ------------------------------------------------------ speaker embeddings --

def _mel_filterbank(num_bins, n_fft, sample_rate, low_freq=20.0, high_freq=0.0):
    import numpy as np
    if high_freq <= 0.0:
        high_freq = sample_rate / 2.0 + high_freq
    num_fft_bins = n_fft // 2

    def mel(f):
        return 1127.0 * np.log(1.0 + np.asarray(f) / 700.0)

    mel_low, mel_high = mel(low_freq), mel(high_freq)
    delta = (mel_high - mel_low) / (num_bins + 1)
    fft_bin_width = sample_rate / n_fft
    freqs = fft_bin_width * np.arange(num_fft_bins)
    mels = mel(freqs)
    bank = np.zeros((num_bins, num_fft_bins + 1), dtype=np.float32)
    for b in range(num_bins):
        left = mel_low + b * delta
        center = mel_low + (b + 1) * delta
        right = mel_low + (b + 2) * delta
        up = (mels - left) / (center - left)
        down = (right - mels) / (right - center)
        bank[b, :num_fft_bins] = np.maximum(0.0, np.minimum(up, down))
    return bank


_FBANK_CACHE = {}


def kaldi_fbank(wave, sample_rate=16000, num_mel_bins=80):
    """numpy approximation of torchaudio.compliance.kaldi.fbank(dither=0)."""
    import numpy as np
    frame_len = int(0.025 * sample_rate)
    shift = int(0.010 * sample_rate)
    n_fft = 1
    while n_fft < frame_len:
        n_fft *= 2
    if len(wave) < frame_len:
        wave = np.pad(wave, (0, frame_len - len(wave)))
    num_frames = 1 + (len(wave) - frame_len) // shift
    idx = np.arange(frame_len)[None, :] + shift * np.arange(num_frames)[:, None]
    frames = wave[idx].astype(np.float64)
    frames -= frames.mean(axis=1, keepdims=True)
    pre = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    frames = frames - 0.97 * pre
    window = np.power(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(frame_len) / (frame_len - 1)), 0.85)
    frames *= window
    spec = np.abs(np.fft.rfft(frames, n=n_fft, axis=1)) ** 2
    key = (num_mel_bins, n_fft, sample_rate)
    if key not in _FBANK_CACHE:
        _FBANK_CACHE[key] = _mel_filterbank(num_mel_bins, n_fft, sample_rate)
    feats = spec @ _FBANK_CACHE[key].T
    feats = np.log(np.maximum(feats, np.finfo(np.float32).eps))
    return feats.astype(np.float32)


class SpeakerEmbedder:
    """CAM++ (campplus.onnx from the CosyVoice model folder) or a log-mel fallback."""

    def __init__(self, campplus_path=None):
        self.kind = "logmel"
        self.session = None
        if campplus_path and os.path.isfile(campplus_path):
            try:
                import onnxruntime
                opts = onnxruntime.SessionOptions()
                opts.intra_op_num_threads = 2
                self.session = onnxruntime.InferenceSession(
                    campplus_path, sess_options=opts, providers=["CPUExecutionProvider"])
                self.kind = "campplus"
            except Exception as exc:  # noqa: BLE001
                log("CAM++ unavailable, using log-mel fallback: %s" % exc)

    def embed(self, wave, sample_rate=16000):
        import numpy as np
        feats = kaldi_fbank(wave, sample_rate)
        if self.session is not None:
            feats = feats - feats.mean(axis=0, keepdims=True)
            name = self.session.get_inputs()[0].name
            vec = self.session.run(None, {name: feats[None, :, :]})[0].reshape(-1)
        else:
            vec = np.concatenate([feats.mean(axis=0), feats.std(axis=0)])
            vec = vec - vec.mean()
        norm = float(np.linalg.norm(vec)) or 1.0
        return (vec / norm).astype(np.float32)


def agglomerative_cluster(emb, n_clusters=0, threshold=0.45):
    """
    Average-linkage agglomerative clustering on cosine similarity.
    Returns a label per row. If n_clusters > 0 merges down to that many,
    otherwise stops when the best pair similarity drops below threshold.
    """
    import numpy as np
    n = emb.shape[0]
    if n == 0:
        return []
    if n == 1:
        return [0]
    sim = emb @ emb.T
    np.fill_diagonal(sim, -np.inf)
    sizes = np.ones(n)
    active = np.ones(n, dtype=bool)
    members = {i: [i] for i in range(n)}
    clusters = n
    target = max(1, int(n_clusters)) if n_clusters else 1
    while clusters > target:
        flat = np.argmax(sim)
        i, j = divmod(int(flat), n)
        best = sim[i, j]
        if not n_clusters and best < threshold:
            break
        if i > j:
            i, j = j, i
        # merge j into i (average linkage)
        wi, wj = sizes[i], sizes[j]
        new_row = (sim[i] * wi + sim[j] * wj) / (wi + wj)
        sim[i, :] = new_row
        sim[:, i] = new_row
        sim[i, i] = -np.inf
        sim[j, :] = -np.inf
        sim[:, j] = -np.inf
        sizes[i] += sizes[j]
        active[j] = False
        members[i].extend(members.pop(j))
        clusters -= 1
    labels = [0] * n
    # order clusters by first appearance in time
    ordered = sorted(members.values(), key=lambda m: min(m))
    for label, m in enumerate(ordered):
        for idx in m:
            labels[idx] = label
    return labels


def assign_speakers(sentences, audio, sr, embedder, n_speakers=0, max_cluster=700):
    import numpy as np
    if not sentences:
        return
    vecs = []
    for s in sentences:
        a = int(max(0, s["start"] - 0.05) * sr)
        b = int(min(len(audio), (s["end"] + 0.05) * sr))
        seg = audio[a:b]
        # CAM++ is happiest with >= 1s; pad short phrases with neighbouring context
        if len(seg) < sr:
            mid = (a + b) // 2
            a2 = max(0, mid - sr // 2)
            seg = audio[a2:a2 + sr]
        seg = seg[: sr * 10]
        vecs.append(embedder.embed(seg, sr))
    emb = np.stack(vecs)

    durations = np.array([s["end"] - s["start"] for s in sentences])
    order = np.argsort(-durations)
    core = np.sort(order[:max_cluster])
    threshold = 0.45 if embedder.kind == "campplus" else 0.92
    core_labels = agglomerative_cluster(emb[core], n_speakers, threshold)

    labels = np.zeros(len(sentences), dtype=int)
    labels[core] = core_labels
    if len(core) < len(sentences):
        k = max(core_labels) + 1
        centroids = np.stack([emb[core][np.array(core_labels) == c].mean(axis=0) for c in range(k)])
        centroids /= np.linalg.norm(centroids, axis=1, keepdims=True) + 1e-9
        rest = np.setdiff1d(np.arange(len(sentences)), core)
        labels[rest] = np.argmax(emb[rest] @ centroids.T, axis=1)

    # renumber by first appearance
    remap = {}
    for lab in labels:
        if int(lab) not in remap:
            remap[int(lab)] = len(remap)
    for s, lab in zip(sentences, labels):
        s["speaker"] = "SPEAKER_%s" % chr(ord("A") + remap[int(lab)]) if remap[int(lab)] < 26 \
            else "SPEAKER_%d" % remap[int(lab)]


def assign_speakers_pyannote(sentences, wav_path, n_speakers=0):
    """Optional: pyannote.audio pipeline (needs `pip install pyannote.audio` and HF_TOKEN)."""
    from pyannote.audio import Pipeline  # noqa: import-outside-toplevel
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=token)
    kwargs = {"num_speakers": int(n_speakers)} if n_speakers else {}
    diar = pipeline(wav_path, **kwargs)
    turns = [(t.start, t.end, spk) for t, _, spk in diar.itertracks(yield_label=True)]
    names = {}
    for s in sentences:
        overlap = {}
        for a, b, spk in turns:
            o = min(b, s["end"]) - max(a, s["start"])
            if o > 0:
                overlap[spk] = overlap.get(spk, 0.0) + o
        spk = max(overlap, key=overlap.get) if overlap else None
        if spk is None:
            s["speaker"] = "SPEAKER_A"
            continue
        if spk not in names:
            names[spk] = "SPEAKER_%s" % chr(ord("A") + min(25, len(names)))
        s["speaker"] = names[spk]


# -------------------------------------------------------------- transcribe --

def cmd_transcribe(args):
    import numpy as np
    register_nvidia_dll_dirs()
    os.makedirs(args.workdir, exist_ok=True)
    progress(0.01, "Извлечение аудио")
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    audio = decode_audio(args.video, sampling_rate=16000)
    sr = 16000
    total = len(audio) / float(sr)
    wav_path = os.path.join(args.workdir, "audio16k.wav")
    try:
        import soundfile as sf
        sf.write(wav_path, audio, sr, subtype="PCM_16")
    except ImportError:
        import wave
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm)

    progress(0.05, "Загрузка Whisper (%s, %s)" % (args.model, args.device))
    try:
        model = WhisperModel(args.model, device=args.device, compute_type=args.compute)
    except Exception as exc:  # noqa: BLE001
        if args.device != "cpu":
            log("Whisper on %s failed (%s), falling back to CPU" % (args.device, exc))
            model = WhisperModel(args.model, device="cpu", compute_type="int8")
        else:
            raise

    language = None if args.language in ("", "auto") else args.language
    progress(0.08, "Распознавание речи")
    segments, info = model.transcribe(
        audio,
        language=language,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        beam_size=5,
        condition_on_previous_text=False,
    )
    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                words.append({"start": float(w.start), "end": float(w.end), "word": w.word})
        else:
            words.append({"start": float(seg.start), "end": float(seg.end), "word": " " + seg.text})
        if total > 0:
            progress(0.08 + 0.72 * min(1.0, float(seg.end) / total),
                     "Распознавание: %d:%02d / %d:%02d" % (
                         int(seg.end) // 60, int(seg.end) % 60, int(total) // 60, int(total) % 60))

    sentences = split_sentences(words, max_gap=args.max_gap)
    progress(0.82, "Определение говорящих")

    diar_kind = "single"
    n_speakers = int(args.speakers or 0)
    if n_speakers == 1:
        for s in sentences:
            s["speaker"] = "SPEAKER_A"
    else:
        done = False
        if args.diarization in ("auto", "pyannote"):
            try:
                assign_speakers_pyannote(sentences, wav_path, n_speakers)
                diar_kind = "pyannote"
                done = True
            except Exception as exc:  # noqa: BLE001
                if args.diarization == "pyannote":
                    log("pyannote failed: %s" % exc)
        if not done:
            embedder = SpeakerEmbedder(args.campplus)
            assign_speakers(sentences, audio, sr, embedder, n_speakers)
            diar_kind = embedder.kind

    for i, s in enumerate(sentences):
        s["id"] = "s%04d" % (i + 1)

    result = {
        "type": "result",
        "language": info.language,
        "language_probability": round(float(info.language_probability or 0.0), 3),
        "duration": round(total, 3),
        "audio16k": wav_path,
        "diarization": diar_kind,
        "segments": sentences,
    }
    with open(os.path.join(args.workdir, "transcript.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    progress(1.0, "Готово: %d предложений" % len(sentences))
    emit(result)


# --------------------------------------------------------------- translate --

def _argos_ensure(source, target):
    import argostranslate.package as pkg
    import argostranslate.translate as tr

    def have(a, b):
        langs = {l.code: l for l in tr.get_installed_languages()}
        if a not in langs or b not in langs:
            return False
        return langs[a].get_translation(langs[b]) is not None

    if have(source, target):
        return
    progress(0.02, "Загрузка языковых пакетов Argos %s→%s" % (source, target))
    pkg.update_package_index()
    available = pkg.get_available_packages()
    wanted = [(source, target)]
    if source != "en" and target != "en":
        wanted += [(source, "en"), ("en", target)]
    for a, b in wanted:
        match = [p for p in available if p.from_code == a and p.to_code == b]
        if match and not have(a, b):
            pkg.install_from_path(match[0].download())
    if not have(source, target):
        raise RuntimeError("Нет пакета Argos для перевода %s→%s" % (source, target))


def cmd_translate(args):
    with open(args.request, "r", encoding="utf-8") as fh:
        req = json.load(fh)
    source = req.get("source") or "en"
    target = req.get("target") or "ru"
    items = req.get("items") or []
    out = []
    if source == target:
        out = [{"id": it["id"], "text": it.get("text", "")} for it in items]
    else:
        _argos_ensure(source, target)
        import argostranslate.translate as tr
        for i, it in enumerate(items):
            text = (it.get("text") or "").strip()
            out.append({"id": it["id"], "text": tr.translate(text, source, target) if text else ""})
            progress((i + 1) / max(1, len(items)), "Перевод %d/%d" % (i + 1, len(items)))
    emit({"type": "result", "items": out, "source": source, "target": target})


def _whisper_repo(model):
    try:
        from faster_whisper.utils import _MODELS
        return _MODELS.get(model, model)
    except Exception:  # noqa: BLE001
        return "Systran/faster-whisper-%s" % model


def whisper_cached(model):
    try:
        from faster_whisper.utils import download_model
        download_model(model, local_files_only=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def cmd_check(args):
    found = {}
    for name in ("numpy", "soundfile", "faster_whisper", "ctranslate2", "argostranslate", "onnxruntime",
                 "pyannote.audio"):
        try:
            __import__(name)
            found[name] = True
        except Exception:  # noqa: BLE001
            found[name] = False
    result = {"type": "result", "python": sys.executable, "version": sys.version.split()[0],
              "modules": found}
    if found["ctranslate2"]:
        register_nvidia_dll_dirs()
        try:
            import ctranslate2
            result["cuda_devices"] = int(ctranslate2.get_cuda_device_count())
        except Exception as exc:  # noqa: BLE001
            result["cuda_devices"] = 0
            result["cuda_error"] = str(exc)
    if found["faster_whisper"] and args.whisper_model:
        result["whisper_model"] = args.whisper_model
        result["whisper_cached"] = whisper_cached(args.whisper_model)
    if found["argostranslate"]:
        try:
            import argostranslate.translate as tr
            langs = tr.get_installed_languages()
            pairs = []
            for a in langs:
                for b in langs:
                    if a.code != b.code and a.get_translation(b) is not None:
                        pairs.append("%s:%s" % (a.code, b.code))
            result["argos_pairs"] = pairs
        except Exception as exc:  # noqa: BLE001
            result["argos_pairs"] = []
            result["argos_error"] = str(exc)
    emit(result)


def cmd_download_whisper(args):
    from faster_whisper.utils import download_model
    progress(0.05, "Скачивание Whisper %s (%s)…" % (args.model, _whisper_repo(args.model)))
    path = download_model(args.model)
    progress(1.0, "Whisper %s готов" % args.model)
    emit({"type": "result", "path": path})


def cmd_install_argos(args):
    _argos_ensure(args.source, args.target)
    emit({"type": "result", "pair": "%s:%s" % (args.source, args.target)})


def main(argv=None):
    p = argparse.ArgumentParser(description="OpenShot dubbing worker")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("transcribe")
    t.add_argument("--video", required=True)
    t.add_argument("--workdir", required=True)
    t.add_argument("--model", default="medium")
    t.add_argument("--device", default="cuda")
    t.add_argument("--compute", default="int8")
    t.add_argument("--language", default="auto")
    t.add_argument("--speakers", type=int, default=0)
    t.add_argument("--max-gap", type=float, default=1.0)
    t.add_argument("--campplus", default="")
    t.add_argument("--diarization", default="auto", choices=["auto", "pyannote", "embedding"])

    tr = sub.add_parser("translate")
    tr.add_argument("--request", required=True)

    ck = sub.add_parser("check")
    ck.add_argument("--whisper-model", default="")

    dw = sub.add_parser("download-whisper")
    dw.add_argument("--model", default="medium")

    ia = sub.add_parser("install-argos")
    ia.add_argument("--source", required=True)
    ia.add_argument("--target", required=True)

    args = p.parse_args(argv)
    try:
        {"transcribe": cmd_transcribe, "translate": cmd_translate, "check": cmd_check,
         "download-whisper": cmd_download_whisper, "install-argos": cmd_install_argos}[args.cmd](args)
        return 0
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        emit({"type": "error", "message": "%s: %s" % (type(exc).__name__, exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
