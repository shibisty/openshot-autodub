"""
 @file
 @brief Puts generated dub phrases on the OpenShot timeline and mixes the original.

 All changes are grouped in one undo transaction, so Ctrl+Z reverts a placement.
 Re-placing first removes the clips from the previous placement.
"""

import json
import os
import uuid

import openshot
from qt_api import QPointF

from classes.app import get_app
from classes.logger import log
from classes.query import Clip, File


def project_fps():
    fps = get_app().project.get("fps") or {"num": 30, "den": 1}
    return float(fps["num"]) / float(fps["den"])


def kf_point(frame, value, interpolation=None):
    interp = openshot.LINEAR if interpolation is None else interpolation
    return json.loads(openshot.Point(float(frame), float(value), interp).Json())


def keyframe(points):
    return {"Points": points}


def source_clip(session):
    """First timeline clip made from the session's video file (lowest position)."""
    if not session.file_id:
        return None
    if session.original_clip_id:
        clip = Clip.get(id=session.original_clip_id)
        if clip:
            return clip
    clips = Clip.filter(file_id=session.file_id)
    if not clips:
        return None
    clips.sort(key=lambda c: float(c.data.get("position", 0.0)))
    return clips[0]


def ensure_source_clip(window, session):
    """Make sure the video is on the timeline; returns the Clip query object."""
    clip = source_clip(session)
    if clip:
        session.original_clip_id = clip.id
        return clip
    numbers = window._track_numbers() if hasattr(window, "_track_numbers") else []
    track = numbers[-1] if numbers else 1000000
    new_clip = window.timeline.addClip(session.file_id, QPointF(0.0, float(track)), int(track),
                                       ignore_refresh=False, call_manual_move=False)
    if isinstance(new_clip, dict) and new_clip.get("id"):
        session.original_clip_id = new_clip["id"]
        return Clip.get(id=new_clip["id"])
    return source_clip(session)


def timeline_seconds(clip_data, source_time):
    return float(clip_data.get("position", 0.0)) + float(source_time) - float(clip_data.get("start", 0.0))


def _make_tracks_above(window, src, count):
    """Create `count` new track numbers stacked directly above the source clip's track."""
    numbers = window._track_numbers()
    layer = int(src.data.get("layer", 1))
    higher = [n for n in numbers if n > layer]
    ceiling = min(higher) if higher else layer + (count + 1) * 1000000
    if ceiling - layer < count + 1:
        window.renumber_all_layers()
        refreshed = Clip.get(id=src.id)
        layer = int((refreshed or src).data.get("layer", 1))
        numbers = window._track_numbers()
        higher = [n for n in numbers if n > layer]
        ceiling = min(higher) if higher else layer + (count + 1) * 1000000
    step = (ceiling - layer) // (count + 1)
    created = []
    for i in range(count):
        created.append(window._create_track(layer + step * (i + 1)))
    return created


def _label_track(number, label):
    from classes.query import Track
    track = Track.get(number=number)
    if track:
        track.data["label"] = label
        track.save()


def remove_previous(session):
    for clip_id in session.placed_clip_ids:
        clip = Clip.get(id=clip_id)
        if clip:
            clip.delete()
    session.placed_clip_ids = []


def restore_original_volume(session):
    clip = Clip.get(id=session.original_clip_id) if session.original_clip_id else None
    if clip and session.original_volume_backup is not None:
        clip.data["volume"] = session.original_volume_backup
        clip.save()


def place_on_timeline(window, session):
    """Returns (placed_count, skipped_count)."""
    app = get_app()
    fps = project_fps()
    app.updates.transaction_id = str(uuid.uuid4())
    try:
        src = ensure_source_clip(window, session)
        if not src:
            raise RuntimeError("Видео не найдено на таймлайне и не может быть добавлено")
        src_data = src.data

        remove_previous(session)

        ready = [s for s in session.segments
                 if s.get("dub_path") and os.path.isfile(s["dub_path"]) and s.get("status") != "error"]
        skipped = len(session.segments) - len(ready)

        # Tracks: one per speaker (or a single one), directly above the video.
        mix = session.mix
        speakers = session.speakers() if mix.get("track_per_speaker", True) else ["__all__"]
        tracks = {}
        existing = [n for n in session.placed_track_numbers if n in window._track_numbers()]
        if len(existing) < len(speakers):
            existing += _make_tracks_above(window, src, len(speakers) - len(existing))
            src = Clip.get(id=src.id) or src
            src_data = src.data
        for speaker, number in zip(speakers, sorted(existing, reverse=True)):
            label = "Дубляж" if speaker == "__all__" else "Дубляж: %s" % speaker
            _label_track(number, label)
            tracks[speaker] = number
        session.placed_track_numbers = list(tracks.values())

        dub_gain = max(0.0, float(mix.get("dub_volume", 100)) / 100.0)
        placed = []
        for seg in ready:
            path = seg["dub_path"]
            if not File.get(path=path):
                window.files_model.add_files(path, quiet=True, prevent_image_seq=True,
                                             prevent_recent_folder=True)
            file_obj = File.get(path=path)
            if not file_obj:
                log.warning("Dubbing: could not import %s", path)
                continue
            layer = tracks.get(seg["speaker"] if "__all__" not in tracks else "__all__")
            if layer is None:
                layer = list(tracks.values())[0]
            pos = max(0.0, timeline_seconds(src_data, seg["start"]))
            pos = round(pos * fps) / fps
            new_clip = window.timeline.addClip(file_obj.id, QPointF(pos, float(layer)), int(layer),
                                               ignore_refresh=True, call_manual_move=False)
            if not isinstance(new_clip, dict):
                continue
            new_clip["volume"] = keyframe([kf_point(1, dub_gain)])
            new_clip["title"] = "%s: %s" % (seg["speaker"], (seg.get("translation") or "")[:40])
            saved = Clip.get(id=new_clip.get("id"))
            if saved:
                saved.data.update(new_clip)
                saved.save()
            seg["clip_id"] = new_clip.get("id", "")
            placed.append(seg)
            session.placed_clip_ids.append(seg["clip_id"])

        _mix_original(src, session, placed, fps)
        window.refreshFilesSignal.emit()
        window.refreshFrameSignal.emit()
        return len(placed), skipped
    finally:
        app.updates.transaction_id = None


def _mix_original(src, session, placed, fps):
    mix = session.mix
    data = src.data
    if session.original_volume_backup is None:
        session.original_volume_backup = data.get("volume") or keyframe([kf_point(1, 1.0)])
    base = 1.0
    low = max(0.0, float(mix.get("original_volume", 25)) / 100.0)

    if not mix.get("overlay", True):
        data["volume"] = keyframe([kf_point(1, 0.0)])          # dub replaces the original
    elif not mix.get("duck_only_speech", True) or not placed:
        data["volume"] = keyframe([kf_point(1, low)])          # constant lower level
    else:
        fade = max(0.02, float(mix.get("duck_fade", 0.15)))
        spans = []
        for seg in sorted(placed, key=lambda s: float(s["start"])):
            a = float(seg["start"])
            b = a + max(float(seg.get("dub_duration") or 0.0), seg.duration)
            if spans and a - fade * 2 <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        points = [kf_point(1, base)]
        last_frame = 1
        for a, b in spans:
            frames = [
                (round((a - fade) * fps) + 1, base),
                (round(a * fps) + 1, low),
                (round(b * fps) + 1, low),
                (round((b + fade) * fps) + 1, base),
            ]
            for frame, value in frames:
                frame = max(last_frame + 1, frame)
                points.append(kf_point(frame, value))
                last_frame = frame
        data["volume"] = keyframe(points)
    src.save()
