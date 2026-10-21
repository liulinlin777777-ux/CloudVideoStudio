"""Versioned, dependency-free project data model for Cloud Video Studio."""

from dataclasses import dataclass, field
from fractions import Fraction
import json
from pathlib import Path
from typing import Any


@dataclass
class Asset:
    id: str
    uri: str
    duration_frames: int


@dataclass
class Effect:
    kind: str
    parameters: dict[str, Any]


@dataclass
class Clip:
    id: str
    asset_id: str
    start_frame: int
    duration_frames: int
    source_in_frame: int = 0
    effects: list[Effect] = field(default_factory=list)


@dataclass
class Track:
    id: str
    kind: str
    clips: list[Clip] = field(default_factory=list)


@dataclass
class Sequence:
    id: str
    width: int
    height: int
    fps_num: int
    fps_den: int
    tracks: list[Track] = field(default_factory=list)

    def frame_time(self, frame: int) -> Fraction:
        """Return the exact elapsed time for ``frame`` frames."""
        if not _is_int(frame) or frame < 0:
            raise ValueError("frame must be a nonnegative integer")
        if not _is_int(self.fps_num) or self.fps_num <= 0:
            raise ValueError("fps_num must be a positive integer")
        if not _is_int(self.fps_den) or self.fps_den <= 0:
            raise ValueError("fps_den must be a positive integer")
        return Fraction(frame * self.fps_den, self.fps_num)


@dataclass
class RenderJob:
    id: str
    sequence_id: str
    output_uri: str
    status: str = "queued"


@dataclass
class Project:
    id: str
    assets: list[Asset] = field(default_factory=list)
    sequences: list[Sequence] = field(default_factory=list)
    render_jobs: list[RenderJob] = field(default_factory=list)
    schema_version: int = 1

    def validate(self) -> None:
        if not _is_int(self.schema_version) or self.schema_version != 1:
            raise ValueError("unsupported project schema_version")
        _require_id(self.id, "project id")
        _require_list(self.assets, "assets")
        _require_list(self.sequences, "sequences")
        _require_list(self.render_jobs, "render_jobs")

        asset_ids: set[str] = set()
        for asset in self.assets:
            if not isinstance(asset, Asset):
                raise ValueError("assets must contain Asset objects")
            _add_unique(asset_ids, asset.id, "asset")
            if not isinstance(asset.uri, str):
                raise ValueError("asset uri must be a string")
            _positive_int(asset.duration_frames, "asset duration_frames")

        sequence_ids: set[str] = set()
        track_ids: set[str] = set()
        clip_ids: set[str] = set()
        for sequence in self.sequences:
            if not isinstance(sequence, Sequence):
                raise ValueError("sequences must contain Sequence objects")
            _add_unique(sequence_ids, sequence.id, "sequence")
            _positive_int(sequence.width, "sequence width")
            _positive_int(sequence.height, "sequence height")
            _positive_int(sequence.fps_num, "sequence fps_num")
            _positive_int(sequence.fps_den, "sequence fps_den")
            _require_list(sequence.tracks, "sequence tracks")
            for track in sequence.tracks:
                if not isinstance(track, Track):
                    raise ValueError("tracks must contain Track objects")
                _add_unique(track_ids, track.id, "track")
                if track.kind not in ("video", "audio"):
                    raise ValueError("track kind must be 'video' or 'audio'")
                _require_list(track.clips, "track clips")
                for clip in track.clips:
                    if not isinstance(clip, Clip):
                        raise ValueError("clips must contain Clip objects")
                    _add_unique(clip_ids, clip.id, "clip")
                    _require_id(clip.asset_id, "clip asset_id")
                    if clip.asset_id not in asset_ids:
                        raise ValueError(f"clip references missing asset: {clip.asset_id}")
                    _nonnegative_int(clip.start_frame, "clip start_frame")
                    _positive_int(clip.duration_frames, "clip duration_frames")
                    _nonnegative_int(clip.source_in_frame, "clip source_in_frame")
                    asset = next(a for a in self.assets if a.id == clip.asset_id)
                    if clip.source_in_frame + clip.duration_frames > asset.duration_frames:
                        raise ValueError("clip source range exceeds asset duration")
                    _require_list(clip.effects, "clip effects")
                    for effect in clip.effects:
                        if not isinstance(effect, Effect):
                            raise ValueError("effects must contain Effect objects")
                        if not isinstance(effect.kind, str):
                            raise ValueError("effect kind must be a string")
                        if not isinstance(effect.parameters, dict):
                            raise ValueError("effect parameters must be a dictionary")

        render_job_ids: set[str] = set()
        for job in self.render_jobs:
            if not isinstance(job, RenderJob):
                raise ValueError("render_jobs must contain RenderJob objects")
            _add_unique(render_job_ids, job.id, "render job")
            _require_id(job.sequence_id, "render job sequence_id")
            if job.sequence_id not in sequence_ids:
                raise ValueError(f"render job references missing sequence: {job.sequence_id}")
            if not isinstance(job.output_uri, str) or not isinstance(job.status, str):
                raise ValueError("render job output_uri and status must be strings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "assets": [
                {"id": asset.id, "uri": asset.uri, "duration_frames": asset.duration_frames}
                for asset in self.assets
            ],
            "sequences": [
                {
                    "id": sequence.id,
                    "width": sequence.width,
                    "height": sequence.height,
                    "fps_num": sequence.fps_num,
                    "fps_den": sequence.fps_den,
                    "tracks": [
                        {
                            "id": track.id,
                            "kind": track.kind,
                            "clips": [
                                {
                                    "id": clip.id,
                                    "asset_id": clip.asset_id,
                                    "start_frame": clip.start_frame,
                                    "duration_frames": clip.duration_frames,
                                    "source_in_frame": clip.source_in_frame,
                                    "effects": [
                                        {"kind": effect.kind, "parameters": effect.parameters}
                                        for effect in clip.effects
                                    ],
                                }
                                for clip in track.clips
                            ],
                        }
                        for track in sequence.tracks
                    ],
                }
                for sequence in self.sequences
            ],
            "render_jobs": [
                {
                    "id": job.id,
                    "sequence_id": job.sequence_id,
                    "output_uri": job.output_uri,
                    "status": job.status,
                }
                for job in self.render_jobs
            ],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        _check_fields(data, {"id", "assets", "sequences", "render_jobs", "schema_version"}, "project")
        assets = [
            Asset(**_checked(item, {"id", "uri", "duration_frames"}, "asset"))
            for item in data.get("assets", [])
        ]
        sequences = []
        for item in data.get("sequences", []):
            _check_fields(item, {"id", "width", "height", "fps_num", "fps_den", "tracks"}, "sequence")
            tracks = []
            for track_data in item.get("tracks", []):
                _check_fields(track_data, {"id", "kind", "clips"}, "track")
                clips = []
                for clip_data in track_data.get("clips", []):
                    _check_fields(
                        clip_data,
                        {"id", "asset_id", "start_frame", "duration_frames", "source_in_frame", "effects"},
                        "clip",
                    )
                    effects = [
                        Effect(**_checked(effect, {"kind", "parameters"}, "effect"))
                        for effect in clip_data.get("effects", [])
                    ]
                    clips.append(Clip(**{**clip_data, "effects": effects}))
                tracks.append(Track(**{**track_data, "clips": clips}))
            sequences.append(Sequence(**{**item, "tracks": tracks}))
        render_jobs = [
            RenderJob(**_checked(item, {"id", "sequence_id", "output_uri", "status"}, "render job"))
            for item in data.get("render_jobs", [])
        ]
        project = cls(
            id=data["id"],
            assets=assets,
            sequences=sequences,
            render_jobs=render_jobs,
            schema_version=data.get("schema_version", 1),
        )
        project.validate()
        return project

    def save(self, path: str | Path) -> None:
        self.validate()
        destination = Path(path)
        destination_resolved = destination.resolve()
        for asset in self.assets:
            # Asset URIs are local paths in this version. Resolve relative paths
            # from the project file, so the safety check uses the same base as
            # a project consumer loading that file.
            source = Path(asset.uri)
            if not source.is_absolute():
                source = destination.parent / source
            source_resolved = source.resolve()
            if destination_resolved == source_resolved:
                raise ValueError("project file cannot overwrite a referenced asset")
            try:
                source_stat = source.stat()
                destination_stat = destination.stat()
            except FileNotFoundError:
                continue
            if (source_stat.st_dev, source_stat.st_ino) == (destination_stat.st_dev, destination_stat.st_ino):
                raise ValueError("project file cannot overwrite a hardlink to a referenced asset")
        destination.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        with Path(path).open("r", encoding="utf-8") as source:
            data = json.load(source)
        return cls.from_dict(data)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: Any, name: str) -> None:
    if not _is_int(value) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _nonnegative_int(value: Any, name: str) -> None:
    if not _is_int(value) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _require_id(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")


def _add_unique(seen: set[str], value: Any, category: str) -> None:
    _require_id(value, f"{category} id")
    if value in seen:
        raise ValueError(f"duplicate {category} id: {value}")
    seen.add(value)


def _require_list(value: Any, name: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")


def _check_fields(data: Any, allowed: set[str], name: str) -> None:
    if not isinstance(data, dict):
        raise TypeError(f"{name} must be an object")
    unknown = data.keys() - allowed
    if unknown:
        raise ValueError(f"unknown {name} field(s): {', '.join(sorted(unknown))}")


def _checked(data: Any, allowed: set[str], name: str) -> dict[str, Any]:
    _check_fields(data, allowed, name)
    return data
