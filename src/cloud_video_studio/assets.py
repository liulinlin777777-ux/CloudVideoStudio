"""Bounded local asset storage with content hashes and validated media metadata.

The store copies user-selected files into its private directory. It never edits
the source file and never resolves an asset from an arbitrary HTTP path.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid
from typing import Any


_MANIFEST = "assets.json"
_MANIFEST_VERSION = 1
_DEFAULT_FILE_LIMIT = 2 * 1024 * 1024 * 1024
_DEFAULT_TOTAL_LIMIT = 10 * 1024 * 1024 * 1024
_PROBE_TIMEOUT_SECONDS = 15
_SUPPORTED = {
    ".mp4": "video", ".mov": "video",
    ".mp3": "audio", ".wav": "audio",
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".srt": "subtitle",
}
_SRT_TIME = re.compile(
    r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3}) --> "
    r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})(?: .*)?$"
)


class AssetStore:
    """Store validated local assets and a reloadable JSON manifest.

    ``max_file_bytes`` and ``max_total_bytes`` are enforced both before and
    while copying. The source must be a regular, non-symlink, single-link file.
    """

    def __init__(
        self,
        root: str | Path,
        max_file_bytes: int = _DEFAULT_FILE_LIMIT,
        max_total_bytes: int = _DEFAULT_TOTAL_LIMIT,
    ) -> None:
        self.root = Path(root).expanduser().absolute()
        self.max_file_bytes = self._limit(max_file_bytes, "max_file_bytes")
        self.max_total_bytes = self._limit(max_total_bytes, "max_total_bytes")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("max_file_bytes cannot exceed max_total_bytes")
        if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
            raise ValueError("asset store root must be a real directory")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("asset store root must not be a symlink")
        self.files = self.root / "files"
        if self.files.exists() and (self.files.is_symlink() or not self.files.is_dir()):
            raise ValueError("asset files path must be a real directory")
        self.files.mkdir(exist_ok=True)
        self.manifest = self.root / _MANIFEST
        if self.manifest.is_symlink():
            raise ValueError("asset manifest must not be a symlink")
        if not self.manifest.exists():
            self._write_manifest([])
        else:
            self._read_manifest()

    @staticmethod
    def _limit(value: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    def import_file(self, path: str | Path) -> dict[str, Any]:
        """Validate, copy, and register a source file, returning its record."""
        source, size = self._source(path)
        suffix = source.suffix.lower()
        kind = _SUPPORTED.get(suffix)
        if kind is None:
            raise ValueError("unsupported file type; choose MP4/MOV, MP3/WAV, JPG/PNG, or SRT")
        metadata = self._inspect(source, kind)
        asset_id = uuid.uuid4().hex
        name = f"{asset_id}{suffix}"
        target = self.files / name
        record = self._record(source, target, asset_id, kind, size, metadata)
        assets = self._read_manifest()
        if sum(item["size_bytes"] for item in assets) + size > self.max_total_bytes:
            raise ValueError("asset store total size limit exceeded")
        self._copy_checked(source, target, size)
        assets.append(record)
        try:
            self._write_manifest(assets)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return dict(record)

    def list_assets(self) -> list[dict[str, Any]]:
        """Return persisted records in stable insertion order."""
        return [dict(item) for item in self._read_manifest()]

    def relink(self, asset_id: str, path: str | Path) -> dict[str, Any]:
        """Replace a missing/broken asset's managed bytes while keeping its ID."""
        assets = self._read_manifest()
        index = next((i for i, item in enumerate(assets) if item["id"] == asset_id), None)
        if index is None:
            raise ValueError("unknown asset id")
        old = assets[index]
        source, size = self._source(path)
        suffix = source.suffix.lower()
        kind = _SUPPORTED.get(suffix)
        if kind != old["kind"]:
            raise ValueError(f"relinked media must have kind {old['kind']}")
        metadata = self._inspect(source, kind)
        new_total = sum(item["size_bytes"] for item in assets) - old["size_bytes"] + size
        if size > self.max_file_bytes or new_total > self.max_total_bytes:
            raise ValueError("asset store size limit exceeded")
        target_name = f"{asset_id}{suffix}"
        target = self.files / target_name
        if target.is_symlink():
            raise ValueError("managed asset path must not be a symlink")
        record = self._record(source, target, asset_id, kind, size, metadata)
        # Stage first so a failed copy leaves the existing asset untouched.
        temporary = self._temporary_path()
        try:
            self._copy_checked(source, temporary, size)
            old_path = self._managed_path(old)
            if old_path != target:
                os.replace(temporary, target)
                temporary = None
                if old_path.exists():
                    old_path.unlink()
            else:
                os.replace(temporary, target)
                temporary = None
            assets[index] = record
            self._write_manifest(assets)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return dict(record)

    def _source(self, path: str | Path) -> tuple[Path, int]:
        source = Path(path).expanduser().absolute()
        try:
            info = source.lstat()
        except OSError as exc:
            raise ValueError("source file is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("source must be a regular file, not a symlink")
        if info.st_nlink != 1:
            raise ValueError("hard-linked source files are not accepted")
        if info.st_size <= 0 or info.st_size > self.max_file_bytes:
            raise ValueError("source file is empty or exceeds the per-file size limit")
        # Use lexical absolute-path containment here. Path.resolve() consults
        # every ancestor (including sandbox-protected system paths) even
        # though lstat above already validates the selected file itself.
        try:
            inside_store = os.path.commonpath((str(source), str(self.root))) == str(self.root)
        except ValueError:
            inside_store = False
        if inside_store:
            raise ValueError("source file must be outside the managed asset store")
        return source, info.st_size

    @staticmethod
    def _record(
        source: Path, target: Path, asset_id: str, kind: str, size: int,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        digest = _hash_file(source)
        return {
            "id": asset_id,
            "filename": source.name,
            "path": target.relative_to(target.parents[1]).as_posix(),
            "sha256": digest,
            "kind": kind,
            "size_bytes": size,
            "metadata": metadata,
        }

    def _inspect(self, source: Path, kind: str) -> dict[str, Any]:
        if kind == "subtitle":
            return _validate_srt(source, self.max_file_bytes)
        suffix = source.suffix.lower()
        if kind == "image":
            with source.open("rb") as stream:
                header = stream.read(8)
            if suffix == ".png" and header != b"\x89PNG\r\n\x1a\n":
                raise ValueError("file extension does not match PNG content")
            if suffix in {".jpg", ".jpeg"} and not header.startswith(b"\xff\xd8\xff"):
                raise ValueError("file extension does not match JPEG content")
        probe = _ffprobe(source)
        streams = probe.get("streams")
        if not isinstance(streams, list) or not streams:
            raise ValueError("FFprobe found no media streams")
        expected = {"video": {"video"}, "audio": {"audio"}, "image": {"video"}}[kind]
        selected = [item for item in streams if isinstance(item, dict) and item.get("codec_type") in expected]
        if not selected:
            raise ValueError(f"file contains no {kind} stream")
        if kind == "video":
            video = selected[0]
            if video.get("codec_name") != "h264":
                raise ValueError("video must use H.264")
            if video.get("profile") not in {"Baseline", "Constrained Baseline", "Main", "High"}:
                raise ValueError("unsupported H.264 profile")
            if probe.get("format", {}).get("format_name", "").split(",")[0] not in {"mov", "mp4"}:
                raise ValueError("video container must be MP4 or MOV")
            width, height = _positive_probe_int(video.get("width"), "video width"), _positive_probe_int(video.get("height"), "video height")
            result: dict[str, Any] = {"codec": "h264", "profile": video["profile"], "width": width, "height": height}
        elif kind == "audio":
            audio = selected[0]
            allowed = {".mp3": {"mp3"}, ".wav": {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le", "pcm_u8"}}
            if audio.get("codec_name") not in allowed[suffix]:
                raise ValueError(f"unsupported audio codec for {suffix}")
            result = {"codec": audio["codec_name"]}
        else:
            image = selected[0]
            codec = image.get("codec_name")
            if suffix == ".png" and codec not in {"png", "apng"}:
                raise ValueError("file extension does not match PNG media")
            if suffix in {".jpg", ".jpeg"} and codec not in {"mjpeg", "jpeg2000"}:
                raise ValueError("file extension does not match JPEG media")
            result = {"codec": codec, "width": _positive_probe_int(image.get("width"), "image width"), "height": _positive_probe_int(image.get("height"), "image height")}
        duration = _duration(probe, selected[0])
        if duration is not None:
            result["duration_seconds"] = duration
        result["format"] = probe.get("format", {}).get("format_name")
        return result

    def _read_manifest(self) -> list[dict[str, Any]]:
        if self.manifest.is_symlink():
            raise ValueError("asset manifest must not be a symlink")
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("asset manifest is invalid") from exc
        if not isinstance(data, dict) or data.get("version") != _MANIFEST_VERSION or not isinstance(data.get("assets"), list):
            raise ValueError("unsupported asset manifest")
        seen: set[str] = set()
        total_bytes = 0
        for item in data["assets"]:
            if not isinstance(item, dict) or set(item) != {"id", "filename", "path", "sha256", "kind", "size_bytes", "metadata"}:
                raise ValueError("invalid asset manifest record")
            if not isinstance(item["id"], str) or item["id"] in seen:
                raise ValueError("invalid or duplicate asset id")
            seen.add(item["id"])
            if item["kind"] not in set(_SUPPORTED.values()) or not isinstance(item["metadata"], dict):
                raise ValueError("invalid asset metadata")
            if not isinstance(item["size_bytes"], int) or item["size_bytes"] <= 0:
                raise ValueError("invalid asset size")
            if item["size_bytes"] > self.max_file_bytes:
                raise ValueError("stored asset exceeds the configured file limit")
            total_bytes += item["size_bytes"]
            self._managed_path(item)
        if total_bytes > self.max_total_bytes:
            raise ValueError("stored assets exceed the configured total size limit")
        return data["assets"]

    def _managed_path(self, record: dict[str, Any]) -> Path:
        relative = Path(record["path"])
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] != "files"
            or relative.parts[1] in {"", ".", ".."}
        ):
            raise ValueError("asset manifest path escapes the store")
        path = self.root / relative
        if path.is_symlink():
            raise ValueError("managed asset must not be a symlink")
        return path

    def _write_manifest(self, assets: list[dict[str, Any]]) -> None:
        if self.manifest.is_symlink():
            raise ValueError("asset manifest must not be a symlink")
        fd, temporary_name = tempfile.mkstemp(prefix=".assets-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": _MANIFEST_VERSION, "assets": assets}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.manifest)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _temporary_path(self) -> Path:
        fd, name = tempfile.mkstemp(prefix=".asset-", dir=self.files)
        os.close(fd)
        path = Path(name)
        path.unlink()
        return path

    def _copy_checked(self, source: Path, target: Path, expected_size: int) -> None:
        if target.exists() or target.is_symlink():
            raise ValueError("asset destination already exists")
        digest = hashlib.sha256()
        copied = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(target, flags, 0o600)
        try:
            with source.open("rb") as incoming, os.fdopen(fd, "wb", closefd=False) as outgoing:
                while True:
                    block = incoming.read(1024 * 1024)
                    if not block:
                        break
                    copied += len(block)
                    if copied > self.max_file_bytes:
                        raise ValueError("source grew beyond the per-file size limit")
                    digest.update(block)
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
        if copied != expected_size:
            target.unlink(missing_ok=True)
            raise ValueError("source changed while it was being imported")
        # Recheck the source after copying to catch replacement or mutation.
        current = source.lstat()
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or current.st_size != expected_size:
            target.unlink(missing_ok=True)
            raise ValueError("source changed while it was being imported")
        if digest.hexdigest() != _hash_file(source):
            target.unlink(missing_ok=True)
            raise ValueError("source changed while it was being imported")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_srt(path: Path, max_bytes: int) -> dict[str, Any]:
    if path.stat().st_size > max_bytes:
        raise ValueError("subtitle exceeds the size limit")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("SRT subtitle must be valid UTF-8") from exc
    if "\x00" in text:
        raise ValueError("SRT subtitle contains invalid NUL characters")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    if not blocks:
        raise ValueError("SRT subtitle has no cues")
    if len(blocks) > 100_000:
        raise ValueError("SRT subtitle has too many cues")
    max_end = 0
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ValueError("invalid SRT cue; expected index, timestamp, and text")
        match = _SRT_TIME.fullmatch(lines[1].strip())
        if match is None:
            raise ValueError("invalid SRT timestamp; expected UTF-8 SRT timecodes")
        values = [int(value) for value in match.groups()]
        start = (((values[0] * 60 + values[1]) * 60 + values[2]) * 1000 + values[3])
        end = (((values[4] * 60 + values[5]) * 60 + values[6]) * 1000 + values[7])
        if start >= end:
            raise ValueError("SRT cue end must follow its start")
        max_end = max(max_end, end)
    return {"encoding": "utf-8", "cue_count": len(blocks), "duration_seconds": max_end / 1000}


def _ffprobe(path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_PROBE_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("FFprobe could not inspect this media file") from exc
    if result.returncode != 0:
        raise ValueError("unsupported or invalid media file; FFprobe rejected it")
    try:
        data = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("FFprobe returned invalid metadata") from exc
    if not isinstance(data, dict) or not isinstance(data.get("format"), dict):
        raise ValueError("FFprobe returned incomplete metadata")
    return data


def _positive_probe_int(value: Any, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"FFprobe returned invalid {name}") from exc
    if number <= 0:
        raise ValueError(f"FFprobe returned invalid {name}")
    return number


def _duration(probe: dict[str, Any], stream: dict[str, Any]) -> float | None:
    value = stream.get("duration", probe.get("format", {}).get("duration"))
    if value in (None, "N/A"):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("FFprobe returned invalid media duration") from exc
    if not 0 < duration < float("inf"):
        raise ValueError("FFprobe returned invalid media duration")
    return duration
