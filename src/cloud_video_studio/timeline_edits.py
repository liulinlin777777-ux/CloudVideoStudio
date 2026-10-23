"""Immutable, non-ripple model edits. Rendering acceptance is separate."""

from copy import deepcopy

from .project import Project


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
