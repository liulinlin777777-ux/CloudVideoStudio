"""Immutable, non-ripple model edits. Rendering acceptance is separate."""

from copy import deepcopy

from .project import Clip, Project, Track


def _validated_project(project: Project) -> Project:
    if not isinstance(project, Project):
        raise ValueError("project must be a Project")
    project.validate()
    return project


def _find_clip(project: Project, clip_id: str):
    for sequence in project.sequences:
        for track in sequence.tracks:
            for clip in track.clips:
                if clip.id == clip_id:
                    return clip
    raise ValueError(f"clip not found: {clip_id}")


def _require_integer(value, name: str, *, positive: bool = False) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be a {qualifier} integer")


def move_clip(project, clip_id, start_frame):
    """Return a validated independent project with one clip moved in time."""
    _validated_project(project)
    _require_integer(start_frame, "start_frame")
    _find_clip(project, clip_id)

    edited = deepcopy(project)
    _find_clip(edited, clip_id).start_frame = start_frame
    edited.validate()
    return edited


def trim_clip(project, clip_id, source_in_frame, duration_frames):
    """Return a validated independent project with one clip's source range trimmed."""
    _validated_project(project)
    _require_integer(source_in_frame, "source_in_frame")
    _require_integer(duration_frames, "duration_frames", positive=True)
    _find_clip(project, clip_id)

    edited = deepcopy(project)
    clip = _find_clip(edited, clip_id)
    clip.source_in_frame = source_in_frame
    clip.duration_frames = duration_frames
    edited.validate()
    return edited


def split_clip(project, clip_id, at_frame, new_clip_id=None):
    """Split a clip at a source-relative frame while preserving timeline placement."""
    _validated_project(project)
    _require_integer(at_frame, "at_frame", positive=True)
    original = _find_clip(project, clip_id)
    if at_frame >= original.duration_frames:
        raise ValueError("split point must be inside the clip")
    crossfade = next((effect for effect in original.effects if effect.kind == "crossfade"), None)
    if crossfade is not None:
        crossfade_frames = crossfade.parameters.get("duration_frames")
        _require_integer(crossfade_frames, "crossfade duration_frames", positive=True)
        if at_frame <= crossfade_frames:
            raise ValueError("split point must be after the clip crossfade")
    edited = deepcopy(project)
    clip = _find_clip(edited, clip_id)
    right_id = new_clip_id or f"{clip.id}-split-{clip.start_frame + at_frame}"
    if not isinstance(right_id, str) or not right_id or any(
        candidate.id == right_id
        for sequence in edited.sequences for track in sequence.tracks for candidate in track.clips
    ):
        raise ValueError("new split clip id must be unique and nonempty")
    right = deepcopy(clip)
    right.id = right_id
    right.start_frame += at_frame
    right.source_in_frame += at_frame
    right.duration_frames -= at_frame
    right.effects = [effect for effect in right.effects if effect.kind != "crossfade"]
    clip.duration_frames = at_frame
    track = next(track for sequence in edited.sequences for track in sequence.tracks if clip in track.clips)
    track.clips.append(right)
    edited.validate()
    return edited


def delete_clip(project, clip_id):
    """Return a copy without the selected clip."""
    _validated_project(project)
    _find_clip(project, clip_id)
    edited = deepcopy(project)
    for sequence in edited.sequences:
        for track in sequence.tracks:
            track.clips = [clip for clip in track.clips if clip.id != clip_id]
            ordered = sorted(track.clips, key=lambda item: (item.start_frame, item.id))
            for index, clip in enumerate(ordered):
                crossfade = next((effect for effect in clip.effects if effect.kind == "crossfade"), None)
                if crossfade is None:
                    continue
                overlap = (
                    ordered[index - 1].start_frame + ordered[index - 1].duration_frames - clip.start_frame
                    if index else 0
                )
                if 0 < overlap < min(ordered[index - 1].duration_frames, clip.duration_frames):
                    crossfade.parameters["duration_frames"] = overlap
                else:
                    clip.effects.remove(crossfade)
    edited.validate()
    return edited


def add_clip(project, asset_id, track_id, start_frame, clip_id):
    """Place a referenced project asset as a full-length clip on a compatible track."""
    _validated_project(project)
    _require_integer(start_frame, "start_frame")
    if not isinstance(clip_id, str) or not clip_id:
        raise ValueError("clip_id must be a nonempty string")
    sequence = project.sequences[0] if len(project.sequences) == 1 else None
    if sequence is None:
        raise ValueError("exactly one sequence is supported")
    track = next((item for item in sequence.tracks if item.id == track_id), None)
    if track is None:
        raise ValueError("track not found")
    asset = next((item for item in project.assets if item.id == asset_id), None)
    if asset is None:
        raise ValueError("asset not found")
    if any(clip.id == clip_id for item in sequence.tracks for clip in item.clips):
        raise ValueError("clip id must be unique")
    if sum(len(item.clips) for item in sequence.tracks) >= 20:
        raise ValueError("sequence already contains twenty media clips")
    timeline_limit = (60 * sequence.fps_num + sequence.fps_den - 1) // sequence.fps_den
    if start_frame >= timeline_limit:
        raise ValueError("start_frame must be inside the 60-second sequence limit")
    asset_kind = asset.metadata.get("kind")
    if asset_kind in {"video", "image"} and track.kind != "video":
        raise ValueError("video and image assets require a video track")
    if asset_kind == "audio" and track.kind != "audio":
        raise ValueError("audio assets require an audio track")
    edited = deepcopy(project)
    target = next(item for item in edited.sequences[0].tracks if item.id == track_id)
    duration = min(
        next(item for item in edited.assets if item.id == asset_id).duration_frames,
        timeline_limit - start_frame,
    )
    target.clips.append(Clip(clip_id, asset_id, start_frame, duration))
    edited.validate()
    return edited


def add_track(project, kind, track_id):
    """Add an empty video or audio track while respecting the frozen track limits."""
    _validated_project(project)
    if kind not in {"video", "audio"}:
        raise ValueError("track kind must be video or audio")
    if not isinstance(track_id, str) or not track_id:
        raise ValueError("track_id must be a nonempty string")
    if any(track.id == track_id for sequence in project.sequences for track in sequence.tracks):
        raise ValueError("track id must be unique")
    sequence = project.sequences[0] if len(project.sequences) == 1 else None
    if sequence is None:
        raise ValueError("exactly one sequence is supported")
    count = sum(track.kind == kind for track in sequence.tracks)
    if count >= 4 or len(sequence.tracks) >= 8:
        raise ValueError("sequence track limit reached")
    edited = deepcopy(project)
    edited.sequences[0].tracks.append(Track(track_id, kind))
    edited.validate()
    return edited


def move_clip_to_track(project, clip_id, start_frame, track_id, *, snap_frames=0):
    """Move a clip in time and optionally to another track of the same media kind."""
    _validated_project(project)
    _require_integer(start_frame, "start_frame")
    _require_integer(snap_frames, "snap_frames")
    source_track = next(
        track for sequence in project.sequences for track in sequence.tracks
        if any(clip.id == clip_id for clip in track.clips)
    )
    target = next(
        (track for sequence in project.sequences for track in sequence.tracks if track.id == track_id),
        None,
    )
    if target is None or target.kind != source_track.kind:
        raise ValueError("target track must exist and have the same media kind")
    clip = _find_clip(project, clip_id)
    if snap_frames:
        boundaries = [0]
        for sequence in project.sequences:
            for track in sequence.tracks:
                for other in track.clips:
                    if other.id != clip_id:
                        boundaries.extend((other.start_frame, other.start_frame + other.duration_frames))
        nearest = min(boundaries, key=lambda frame: (abs(frame - start_frame), frame))
        if abs(nearest - start_frame) <= snap_frames:
            start_frame = nearest
    edited = deepcopy(project)
    moving = _find_clip(edited, clip_id)
    from_track = next(
        track for sequence in edited.sequences for track in sequence.tracks
        if any(item.id == clip_id for item in track.clips)
    )
    to_track = next(track for sequence in edited.sequences for track in sequence.tracks if track.id == track_id)
    if from_track is not to_track:
        if len(to_track.clips) >= 20:
            raise ValueError("target track is full")
        from_track.clips.remove(moving)
        to_track.clips.append(moving)
    moving.start_frame = start_frame
    edited.validate()
    return edited


def reorder_clip(project, clip_id, before_clip_id=None):
    """Place a clip immediately before another clip on its current track in timeline time."""
    _validated_project(project)
    clip = _find_clip(project, clip_id)
    track = next(track for sequence in project.sequences for track in sequence.tracks if clip in track.clips)
    ordered = sorted(track.clips, key=lambda item: (item.start_frame, item.id))
    if before_clip_id is None:
        ordered = [item for item in ordered if item.id != clip_id] + [clip]
    else:
        before = _find_clip(project, before_clip_id)
        if before_clip_id == clip_id or before not in track.clips:
            raise ValueError("reorder target must be another clip on the same track")
        ordered.remove(clip)
        ordered.insert(ordered.index(before), clip)
    edited = deepcopy(project)
    edited_track = next(item for sequence in edited.sequences for item in sequence.tracks if item.id == track.id)
    ordered_ids = [item.id for item in ordered]
    edited_by_id = {item.id: item for item in edited_track.clips}
    before_start = _find_clip(project, before_clip_id).start_frame if before_clip_id is not None else None
    cursor = 0
    for item_id in ordered_ids:
        item = edited_by_id[item_id]
        if item_id == clip_id and before_start is not None:
            item.start_frame = max(cursor, before_start - item.duration_frames)
        else:
            item.start_frame = max(cursor, item.start_frame)
        cursor = item.start_frame + item.duration_frames
    edited.validate()
    return edited
