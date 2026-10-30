"""Bounded local asset storage with content hashes and validated media metadata.

The store copies user-selected files into its private directory. It never edits
the source file and never resolves an asset from an arbitrary HTTP path.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import fcntl
from contextlib import contextmanager
from fractions import Fraction
from functools import wraps
from typing import Any


_MANIFEST = "assets.json"
_MANIFEST_VERSION = 2
_DEFAULT_FILE_LIMIT = 2 * 1024 * 1024 * 1024
_DEFAULT_TOTAL_LIMIT = 10 * 1024 * 1024 * 1024
_DERIVATIVE_TOTAL_LIMIT = 512 * 1024 * 1024
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
_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _store_transaction(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock, self._process_lock():
            return method(self, *args, **kwargs)
    return locked


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
        ffprobe: str = "ffprobe",
    ) -> None:
        if not isinstance(ffprobe, str) or not ffprobe:
            raise ValueError("ffprobe executable must be a non-empty string")
        self.ffprobe = ffprobe
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        self._reject_symlink_ancestors(self.root)
        with _STORE_LOCKS_GUARD:
            self._lock = _STORE_LOCKS.setdefault(str(self.root), threading.RLock())
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
        self.lockfile = self.root / ".assets.lock"
        with self._lock:
            with self._process_lock():
                if self.manifest.is_symlink():
                    raise ValueError("asset manifest must not be a symlink")
                if not self.manifest.exists():
                    self._write_manifest([])
                else:
                    self._read_manifest()

    @staticmethod
    def _reject_symlink_ancestors(path: Path) -> None:
        target = Path(os.path.abspath(path))
        trusted_anchors = (Path(tempfile.gettempdir()), Path.cwd())
        containing = []
        for candidate in trusted_anchors:
            try:
                if os.path.commonpath((str(target), str(candidate))) == str(candidate):
                    containing.append(candidate)
            except ValueError:
                continue
        if containing:
            anchor = max(containing, key=lambda item: len(item.parts))
        else:
            anchor = Path(target.anchor)
        try:
            relative_parts = target.relative_to(anchor).parts
        except ValueError as exc:
            raise ValueError("path is outside the trusted runtime anchor") from exc
        current = anchor
        for component in (".", *relative_parts):
            if component != ".":
                current /= component
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValueError("path descendant is unavailable") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("symlink path ancestors are not accepted")

    @contextmanager
    def _process_lock(self, timeout: float | None = None):
        if (timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                     or not math.isfinite(timeout) or timeout < 0)):
            raise ValueError("asset lock timeout must be finite and non-negative")
        if self.lockfile.is_symlink():
            raise ValueError("asset lock must not be a symlink")
        descriptor = os.open(self.lockfile, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        acquired = False
        try:
            lock_info = os.fstat(descriptor)
            if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                raise ValueError("asset lock must be a regular, unaliased file")
            if timeout is None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                acquired = True
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("asset store lock deadline exceeded")
                        time.sleep(min(0.01, remaining))
            yield
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _limit(value: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @_store_transaction
    def import_file(self, path: str | Path, *, filename: str | None = None) -> dict[str, Any]:
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
        if filename is not None:
            if (not isinstance(filename, str) or not filename or Path(filename).name != filename
                    or filename in {'.', '..'} or '/' in filename or '\\' in filename
                    or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
                raise ValueError("invalid display filename")
            record["filename"] = filename
        assets = self._read_manifest()
        if sum(item["size_bytes"] for item in assets) + size > self.max_total_bytes:
            raise ValueError("asset store total size limit exceeded")
        digest = self._copy_checked(source, target, size)
        metadata = self._inspect(target, kind)
        record["sha256"] = digest
        record["metadata"] = metadata
        assets.append(record)
        try:
            self._write_manifest(assets)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return dict(record)

    @_store_transaction
    def list_assets(self) -> list[dict[str, Any]]:
        """Return persisted records in stable insertion order."""
        return [dict(item) for item in self._read_manifest()]

    @_store_transaction
    def verified_asset(self, asset_id: str, *, kind: str | None = None) -> tuple[dict[str, Any], Path]:
        """Return a managed asset only after checking its bytes and probe metadata."""
        record = self._asset(asset_id)
        if kind is not None and record["kind"] != kind:
            raise ValueError(f"asset must be {kind}")
        path = self._managed_path(record)
        self._verify_managed(path, record)
        return dict(record), path

    @_store_transaction
    def create_thumbnail(self, asset_id: str, *, ffmpeg: str = "ffmpeg", timeout: float = 20) -> dict[str, Any]:
        """Create a small JPEG derivative for a validated video or still image."""
        record = self._asset(asset_id)
        if record["kind"] not in {"video", "image"}:
            raise ValueError("thumbnails require a video or image asset")
        source = self._managed_path(record)
        self._verify_managed(source, record)
        target = self._derivative_path(asset_id, "thumbnail.jpg")
        output_limit = self._derivative_limit(target, 5 * 1024 * 1024)
        try:
            self._run_ffmpeg([
                ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(source), "-frames:v", "1",
                "-vf", "scale=640:360:force_original_aspect_ratio=decrease", "-threads", "1", "-fs", str(output_limit), str(target),
            ], timeout, "thumbnail generation")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        result = self._check_derivative(target, "image/jpeg", output_limit)
        self._check_derivative_total()
        return result

    @_store_transaction
    def create_proxy(self, asset_id: str, *, ffmpeg: str = "ffmpeg", timeout: float = 120) -> dict[str, Any]:
        """Create a bounded 640x360 H.264/AAC derivative for a validated video."""
        record = self._asset(asset_id)
        if record["kind"] != "video":
            raise ValueError("proxies require a video asset")
        source = self._managed_path(record)
        self._verify_managed(source, record)
        target = self._derivative_path(asset_id, "proxy.mp4")
        output_limit = self._derivative_limit(target, 256 * 1024 * 1024)
        try:
            self._run_ffmpeg([
                ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(source),
                "-vf", "scale=640:360:force_original_aspect_ratio=decrease", "-t", "600",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-threads", "2",
                "-fs", str(output_limit), str(target),
            ], timeout, "proxy generation")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        result = self._check_derivative(target, "video/mp4", output_limit)
        try:
            probe = _ffprobe(target, executable=self.ffprobe)
        except ValueError:
            target.unlink(missing_ok=True)
            raise
        streams = probe.get("streams", [])
        video = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None)
        if video is None or video.get("codec_name") != "h264":
            target.unlink(missing_ok=True)
            raise ValueError("proxy output metadata is invalid")
        width, height = _dimensions(video.get("width"), video.get("height"), "proxy")
        result["metadata"] = {"codec": "h264", "width": width, "height": height}
        self._check_derivative_total()
        return result

    def _asset(self, asset_id: str) -> dict[str, Any]:
        for record in self._read_manifest():
            if record["id"] == asset_id:
                return record
        raise ValueError("unknown asset id")

    def _verify_managed(self, path: Path, record: dict[str, Any]) -> None:
        if not path.is_file() or path.stat().st_size != record["size_bytes"] or _hash_file(path) != record["sha256"]:
            raise ValueError("managed asset is missing or failed its content hash")
        if self._inspect(path, record["kind"]) != record["metadata"]:
            raise ValueError("managed asset metadata no longer matches its validated probe")

    def _derivative_path(self, asset_id: str, suffix: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", asset_id):
            raise ValueError("invalid asset id")
        directory = self.root / "derivatives"
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise ValueError("derivative directory must be a real directory")
        directory.mkdir(exist_ok=True)
        target = directory / f"{asset_id}-{suffix}"
        if target.is_symlink():
            raise ValueError("derivative target must not be a symlink")
        target.unlink(missing_ok=True)
        return target

    def _derivative_limit(self, target: Path, per_file_limit: int) -> int:
        used = sum(path.stat().st_size for path in (self.root / "derivatives").iterdir()
                   if path.is_file() and not path.is_symlink() and path != target)
        available = _DERIVATIVE_TOTAL_LIMIT - used
        if available <= 0:
            raise ValueError("asset derivative storage limit exceeded")
        return min(per_file_limit, available)

    def _check_derivative_total(
        self,
        *,
        replacing: set[str] | None = None,
        additional_bytes: int = 0,
    ) -> None:
        if (isinstance(additional_bytes, bool) or not isinstance(additional_bytes, int)
                or additional_bytes < 0):
            raise ValueError("invalid derivative size reservation")
        excluded = replacing or set()
        if (not isinstance(excluded, set)
                or any(not isinstance(name, str) or Path(name).name != name for name in excluded)):
            raise ValueError("invalid derivative replacement list")
        used = sum(path.stat().st_size for path in (self.root / "derivatives").iterdir()
                   if path.is_file() and not path.is_symlink() and path.name not in excluded)
        if used + additional_bytes > _DERIVATIVE_TOTAL_LIMIT:
            raise ValueError("asset derivative storage limit exceeded")

    @staticmethod
    def _run_ffmpeg(command: list[str], timeout: float, operation: str) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise ValueError("derivative timeout must be greater than 0 and at most 120 seconds")
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"{operation} failed or timed out") from exc
        if result.returncode:
            raise ValueError(f"{operation} failed")

    def _check_derivative(self, path: Path, content_type: str, maximum: int) -> dict[str, Any]:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ValueError("derivative output was not created") from exc
        if path.is_symlink() or not path.is_file() or size <= 0 or size > maximum:
            path.unlink(missing_ok=True)
            raise ValueError("derivative output is empty or exceeds its size limit")
        if content_type == "image/jpeg":
            with path.open("rb") as image:
                valid = image.read(3) == b"\xff\xd8\xff"
        else:
            try:
                probe = _ffprobe(path, executable=self.ffprobe)
            except ValueError:
                path.unlink(missing_ok=True)
                raise
            streams = probe.get("streams", [])
            valid = any(isinstance(s, dict) and s.get("codec_type") == "video" and s.get("codec_name") == "h264" for s in streams)
            formats = probe.get("format", {}).get("format_name", "")
            valid = valid and isinstance(formats, str) and "mp4" in formats.split(",")
        if not valid:
            path.unlink(missing_ok=True)
            raise ValueError("derivative output failed format validation")
        return {"path": path.relative_to(path.parents[1]).as_posix(), "size_bytes": size, "content_type": content_type}

    @_store_transaction
    def relink(self, asset_id: str, path: str | Path, *, filename: str | None = None) -> dict[str, Any]:
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
        target_name = f"{asset_id}-{uuid.uuid4().hex}{suffix}"
        target = self.files / target_name
        if target.is_symlink():
            raise ValueError("managed asset path must not be a symlink")
        old_path = self._managed_path(old)
        if target != old_path and target.exists():
            raise ValueError("relink destination already exists")
        record = self._record(source, target, asset_id, kind, size, metadata)
        if filename is not None:
            if (not isinstance(filename, str) or not filename or Path(filename).name != filename
                    or filename in {'.', '..'} or '/' in filename or '\\' in filename
                    or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
                raise ValueError("invalid display filename")
            record["filename"] = filename
        # Keep the prior managed file until the new manifest is committed.
        temporary = self._temporary_path()
        try:
            digest = self._copy_checked(source, temporary, size)
            metadata = self._inspect(temporary, kind)
            record["sha256"] = digest
            record["metadata"] = metadata
            os.replace(temporary, target)
            temporary = None
            assets[index] = record
            try:
                self._write_manifest(assets)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                pass
            cleanup_pending = False
            try:
                self._remove_analysis_derivatives(asset_id)
            except (OSError, ValueError):
                cleanup_pending = True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        result = dict(record)
        result["analysis_cleanup_pending"] = cleanup_pending
        return result

    def _remove_analysis_derivatives(self, asset_id: str) -> None:
        """Discard cached analysis files before a relinked record is published."""
        if not re.fullmatch(r"[0-9a-f]{32}", asset_id):
            raise ValueError("invalid asset id")
        directory = self.root / "derivatives"
        try:
            directory_info = directory.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(directory_info.st_mode):
            raise ValueError("asset derivative directory must be a real directory")
        cache_name = f"{asset_id}-analysis.json"
        keyframe_name = re.compile(
            rf"{re.escape(asset_id)}-analysis-[0-9a-f]{{12}}-\d{{3}}\.jpg"
        )
        for derivative in directory.iterdir():
            if derivative.name != cache_name and not keyframe_name.fullmatch(derivative.name):
                continue
            try:
                info = derivative.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                derivative.unlink(missing_ok=True)
            else:
                raise ValueError("analysis derivative is not a regular file")

    def _source(self, path: str | Path) -> tuple[Path, int]:
        source = Path(path).expanduser().absolute()
        try:
            info = source.lstat()
        except OSError as exc:
            raise ValueError("source file is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("source must be a regular file, not a symlink")
        self._reject_symlink_ancestors(source.parent)
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
            "metadata_version": 1,
        }

    def _inspect(
        self, source: Path, kind: str, *, probe_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
        probe = probe_data if probe_data is not None else _ffprobe(source, executable=self.ffprobe)
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
            width, height = _dimensions(video.get("width"), video.get("height"), "video")
            result: dict[str, Any] = {"codec": "h264", "profile": video["profile"], "width": width, "height": height}
            frame_rate = _frame_rate(video.get("avg_frame_rate")) or _frame_rate(video.get("r_frame_rate"))
            if frame_rate is not None:
                result["fps_num"], result["fps_den"] = frame_rate
        elif kind == "audio":
            audio = selected[0]
            allowed = {".mp3": {"mp3"}, ".wav": {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le", "pcm_u8"}}
            if audio.get("codec_name") not in allowed[suffix]:
                raise ValueError(f"unsupported audio codec for {suffix}")
            result = {
                "codec": audio["codec_name"],
                "sample_rate": _positive_probe_int(audio.get("sample_rate"), "sample rate"),
                "channels": _positive_probe_int(audio.get("channels"), "channel count"),
            }
        else:
            image = selected[0]
            codec = image.get("codec_name")
            if suffix == ".png" and codec not in {"png", "apng"}:
                raise ValueError("file extension does not match PNG media")
            if suffix in {".jpg", ".jpeg"} and codec not in {"mjpeg", "jpeg2000"}:
                raise ValueError("file extension does not match JPEG media")
            width, height = _dimensions(image.get("width"), image.get("height"), "image")
            result = {"codec": codec, "width": width, "height": height}
        duration = _duration(probe, selected[0])
        if duration is not None:
            result["duration_seconds"] = duration
        result["format"] = probe.get("format", {}).get("format_name")
        result["streams"] = [
            {
                key: value for key, value in {
                    "index": _stream_index(stream.get("index")),
                    "type": stream.get("codec_type"),
                    "codec": stream.get("codec_name"),
                    "profile": stream.get("profile"),
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "duration_seconds": _duration(probe, stream),
                    "fps": _json_frame_rate(
                        _frame_rate(stream.get("avg_frame_rate"))
                        or _frame_rate(stream.get("r_frame_rate"))
                    ),
                    "sample_rate": _positive_probe_int(stream.get("sample_rate"), "sample rate")
                    if stream.get("codec_type") == "audio" else None,
                    "channels": _positive_probe_int(stream.get("channels"), "channel count")
                    if stream.get("codec_type") == "audio" else None,
                }.items() if value is not None
            }
            for stream in streams if isinstance(stream, dict)
        ]
        return result

    def _read_manifest(self) -> list[dict[str, Any]]:
        if self.manifest.is_symlink():
            raise ValueError("asset manifest must not be a symlink")
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("asset manifest is invalid") from exc
        if not isinstance(data, dict) or data.get("version") not in (1, _MANIFEST_VERSION) or not isinstance(data.get("assets"), list):
            raise ValueError("unsupported asset manifest")
        seen: set[str] = set()
        total_bytes = 0
        for item in data["assets"]:
            if not isinstance(item, dict) or set(item) not in (
                {"id", "filename", "path", "sha256", "kind", "size_bytes", "metadata"},
                {"id", "filename", "path", "sha256", "kind", "size_bytes", "metadata", "metadata_version"},
            ):
                raise ValueError("invalid asset manifest record")
            if not isinstance(item["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", item["id"]) or item["id"] in seen:
                raise ValueError("invalid or duplicate asset id")
            seen.add(item["id"])
            if not isinstance(item["kind"], str) or item["kind"] not in set(_SUPPORTED.values()) or not isinstance(item["metadata"], dict):
                raise ValueError("invalid asset metadata")
            filename = item["filename"]
            if (not isinstance(filename, str) or not filename or Path(filename).name != filename
                    or '/' in filename or '\\' in filename or filename in {'.', '..'}):
                raise ValueError("invalid asset filename")
            if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise ValueError("invalid asset content digest")
            item.setdefault("metadata_version", 1)
            if isinstance(item["metadata_version"], bool) or item["metadata_version"] != 1:
                raise ValueError("unsupported asset metadata version")
            if isinstance(item["size_bytes"], bool) or not isinstance(item["size_bytes"], int) or item["size_bytes"] <= 0:
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

    def _copy_checked(self, source: Path, target: Path, expected_size: int) -> str:
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
        copied_hash = digest.hexdigest()
        if copied_hash != _hash_file(source):
            target.unlink(missing_ok=True)
            raise ValueError("source changed while it was being imported")
        return copied_hash


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


def _ffprobe(path: Path, *, executable: str = "ffprobe") -> dict[str, Any]:
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_entries",
             "format=format_name,duration:stream=index,codec_type,codec_name,profile,width,height,duration,avg_frame_rate,r_frame_rate,sample_rate,channels",
             "-show_format", "-show_streams", "-of", "json", str(path)],
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


def _stream_index(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("FFprobe returned an invalid stream index")
    try:
        index = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("FFprobe returned an invalid stream index") from exc
    if index < 0 or str(index) != str(value).strip():
        raise ValueError("FFprobe returned an invalid stream index")
    return index


def _json_frame_rate(value: tuple[int, int] | None) -> list[int] | None:
    return list(value) if value is not None else None


def _dimensions(width_value: Any, height_value: Any, name: str) -> tuple[int, int]:
    width = _positive_probe_int(width_value, f"{name} width")
    height = _positive_probe_int(height_value, f"{name} height")
    if width > 16_384 or height > 16_384 or width * height > 100_000_000:
        raise ValueError(f"FFprobe returned unsupported {name} dimensions")
    return width, height


def _duration(probe: dict[str, Any], stream: dict[str, Any]) -> float | None:
    value = stream.get("duration", probe.get("format", {}).get("duration"))
    if value in (None, "N/A"):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("FFprobe returned invalid media duration") from exc
    if not 0 < duration <= 24 * 60 * 60:
        raise ValueError("FFprobe returned invalid media duration")
    return duration


def _frame_rate(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, str) or value in {"", "0/0", "N/A"}:
        return None
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise ValueError("FFprobe returned invalid frame rate")
    if rate <= 0 or rate > 1000:
        raise ValueError("FFprobe returned unsupported frame rate")
    return rate.numerator, rate.denominator
