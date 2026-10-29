"""Local, project-bound video metadata, scene and keyframe analysis.

The implementation delegates media decoding to the user's installed FFmpeg
and FFprobe binaries. It does not upload footage or infer speech transcripts.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import tempfile
import time
import uuid
from typing import Any

from .assets import AssetStore


_ASSET_ID = re.compile(r"^[0-9a-f]{32}$")
_SCENE_LINE = re.compile(
    r"frame:\s*\d+\s+pts:\s*(?P<pts>-?\d+)\s+pts_time:(?P<time>-?\d+(?:\.\d+)?)\s*\n"
    r"lavfi\.scene_score=(?P<score>\d+(?:\.\d+)?)"
)
_CACHE_SCHEMA = 3
_CACHE_BYTES_LIMIT = 1024 * 1024
_KEYFRAME_BYTES_LIMIT = 2 * 1024 * 1024
_KEYFRAME_COUNT_LIMIT = 25
_SCENE_COUNT_LIMIT = 24
_ANALYSIS_SECONDS_LIMIT = 600.0
_ANALYSIS_OUTPUT_LIMIT = 80 * 1024 * 1024
_VERIFIED_SOURCE_CACHE_LIMIT = 256
_TAG_KEYS = ("title", "artist", "date", "encoder")


class _OutputLimitExceeded(Exception):
    """A child process exceeded a bounded stdout or stderr allowance."""


def _bounded_subprocess(
    command: list[str], *, timeout: float, stdout_limit: int, stderr_limit: int,
) -> subprocess.CompletedProcess[str]:
    """Capture bounded child output and terminate the child as soon as it overflows."""
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    limits = {"stdout": stdout_limit, "stderr": stderr_limit}
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    exceeded: str | None = None
    exceeded_at: float | None = None
    timed_out = False
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    try:
        for name, stream in streams.items():
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map() or process.poll() is None:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0 and not timed_out and process.poll() is None:
                timed_out = True
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            if (exceeded_at is not None and process.poll() is None
                    and time.monotonic() - exceeded_at >= 0.25):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            events = selector.select(max(0.0, min(0.05, remaining))) if selector.get_map() else []
            for key, _ in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                name = key.data
                captured = buffers[name]
                allowance = limits[name] + 1 - len(captured)
                if allowance > 0:
                    captured.extend(chunk[:allowance])
                if len(captured) > limits[name] and exceeded is None:
                    exceeded = name
                    exceeded_at = time.monotonic()
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
        process.wait()
    finally:
        selector.close()
        for stream in streams.values():
            if not stream.closed:
                stream.close()
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()
    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout)
    if exceeded is not None:
        raise _OutputLimitExceeded(exceeded)
    return subprocess.CompletedProcess(
        command, process.returncode,
        buffers["stdout"].decode("utf-8", errors="replace"),
        buffers["stderr"].decode("utf-8", errors="replace"),
    )


class MediaAnalysisError(ValueError):
    """A bounded media analysis failed or its result was invalid."""


def _cache_payload(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _finite_positive(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _ratio(value: Any) -> list[int] | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    if result <= 0 or result.numerator > 1_000_000 or result.denominator > 1_000_000:
        return None
    return [result.numerator, result.denominator]


def _safe_tag(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text[:240]


def _derivatives_dir(store: AssetStore) -> Path:
    directory = store.root / "derivatives"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise MediaAnalysisError("analysis derivative directory is unavailable")
    directory.mkdir(exist_ok=True)
    if directory.is_symlink():
        raise MediaAnalysisError("analysis derivative directory must not be a symlink")
    return directory


def _cache_path(store: AssetStore, asset_id: str) -> Path:
    if not _ASSET_ID.fullmatch(asset_id):
        raise MediaAnalysisError("invalid asset id")
    path = _derivatives_dir(store) / f"{asset_id}-analysis.json"
    if path.is_symlink():
        raise MediaAnalysisError("analysis cache must not be a symlink")
    return path


def _load_analysis(store: AssetStore, asset_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
    path = _cache_path(store, asset_id)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MediaAnalysisError("analysis cache is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0 or info.st_size > _CACHE_BYTES_LIMIT:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (not isinstance(data, dict) or data.get("schema_version") != _CACHE_SCHEMA
            or data.get("asset_id") != asset_id or data.get("source_sha256") != record.get("sha256")
            or not isinstance(data.get("scenes"), list) or not isinstance(data.get("metadata"), dict)
            or data["metadata"].get("filename") != record.get("filename")
            or data["metadata"].get("size_bytes") != record.get("size_bytes")
            or not isinstance(data.get("scene_detection"), dict)):
        return None
    keyframes = data.get("keyframes")
    if not isinstance(keyframes, list) or not 1 <= len(keyframes) <= _KEYFRAME_COUNT_LIMIT:
        return None
    run_id = data.get("analysis_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{12}", run_id):
        return None
    if len(data["scenes"]) != len(keyframes):
        return None
    for index, name in enumerate(keyframes):
        expected = f"{asset_id}-analysis-{run_id}-{index:03d}.jpg"
        image = path.parent / expected
        if name != expected:
            return None
        try:
            image_info = image.lstat()
        except OSError:
            return None
        if (not stat.S_ISREG(image_info.st_mode) or image.is_symlink() or image_info.st_nlink != 1
                or not 3 <= image_info.st_size <= _KEYFRAME_BYTES_LIMIT):
            return None
        try:
            with image.open("rb") as stream:
                if stream.read(3) != b"\xff\xd8\xff":
                    return None
        except OSError:
            return None
    return data


def _source_fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise MediaAnalysisError("ANALYSIS_DEADLINE_EXCEEDED")


def _hash_source_under_deadline(
    path: Path, deadline: float | None, *, expected_fingerprint: tuple[int, int, int, int, int, int, int] | None = None,
) -> str:
    digest = hashlib.sha256()
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (expected_fingerprint is not None
                    and _source_fingerprint(opened) != expected_fingerprint)):
            raise ValueError("managed asset changed during content verification")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            while True:
                _check_deadline(deadline)
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise ValueError("managed asset is missing or failed its content hash") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _check_deadline(deadline)
    return digest.hexdigest()


def _verify_managed_bytes_under_store_lock(
    store: AssetStore, record: dict[str, Any], *, deadline: float | None = None,
) -> Path:
    """Verify source bytes once per unchanged filesystem fingerprint.

    The store locks serialize application mutations. Device, inode, size, mtime,
    and ctime detect external replacement or modification between reads; a new
    fingerprint always triggers a full SHA-256 check before it is cached.
    """
    _check_deadline(deadline)
    source = store._managed_path(record)
    try:
        info = source.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size != record["size_bytes"]):
            raise ValueError("managed asset is missing or failed its content hash")
    except OSError as exc:
        raise ValueError("managed asset is missing or failed its content hash") from exc
    fingerprint = _source_fingerprint(info)
    key = record["id"]
    cache = getattr(store, "_media_verified_source_fingerprints", None)
    if not isinstance(cache, dict):
        cache = {}
        store._media_verified_source_fingerprints = cache
    cached = cache.get(key)
    if cached == (record.get("path"), record.get("sha256"), fingerprint):
        _check_deadline(deadline)
        cache.pop(key, None)
        cache[key] = cached
        return source
    if _hash_source_under_deadline(
            source, deadline, expected_fingerprint=fingerprint,
    ) != record["sha256"]:
        raise ValueError("managed asset is missing or failed its content hash")
    try:
        after = source.lstat()
    except OSError as exc:
        raise ValueError("managed asset is missing or failed its content hash") from exc
    if _source_fingerprint(after) != fingerprint:
        raise ValueError("managed asset changed during content verification")
    cache[key] = (record.get("path"), record.get("sha256"), fingerprint)
    while len(cache) > _VERIFIED_SOURCE_CACHE_LIMIT:
        cache.pop(next(iter(cache)))
    _check_deadline(deadline)
    return source


def _analysis_under_store_lock(store: AssetStore, asset_id: str) -> dict[str, Any] | None:
    if not _ASSET_ID.fullmatch(asset_id):
        raise MediaAnalysisError("invalid asset id")
    record = next((item for item in store._read_manifest() if item["id"] == asset_id), None)
    if record is None:
        raise MediaAnalysisError("unknown asset id")
    if record["kind"] != "video":
        raise MediaAnalysisError("analysis is available for video assets only")
    _verify_managed_bytes_under_store_lock(store, record)
    return _load_analysis(store, asset_id, record)


def get_analysis(store: AssetStore, asset_id: str) -> dict[str, Any] | None:
    """Read a complete cache snapshot while relinks and analysis publication are excluded."""
    if not _ASSET_ID.fullmatch(asset_id):
        raise MediaAnalysisError("invalid asset id")
    with store._lock, store._process_lock():
        return _analysis_under_store_lock(store, asset_id)


def _keyframe_path_under_store_lock(
    store: AssetStore, asset_id: str, index: int, analysis_id: str | None,
) -> Path:
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < _KEYFRAME_COUNT_LIMIT:
        raise MediaAnalysisError("invalid keyframe index")
    if analysis_id is not None and (not isinstance(analysis_id, str)
                                    or not re.fullmatch(r"[0-9a-f]{12}", analysis_id)):
        raise MediaAnalysisError("invalid analysis id")
    analysis = _analysis_under_store_lock(store, asset_id)
    if analysis is None or index >= len(analysis["keyframes"]):
        raise MediaAnalysisError("keyframe not found")
    if analysis_id is not None and analysis["analysis_id"] != analysis_id:
        raise MediaAnalysisError("analysis changed; reload the scene list")
    path = _derivatives_dir(store) / analysis["keyframes"][index]
    if path.is_symlink() or not path.is_file():
        raise MediaAnalysisError("keyframe not found")
    return path


def keyframe_path(
    store: AssetStore, asset_id: str, index: int, *, analysis_id: str | None = None,
) -> Path:
    """Resolve a scene frame; callers serving bytes should use read_keyframe instead."""
    with store._lock, store._process_lock():
        return _keyframe_path_under_store_lock(store, asset_id, index, analysis_id)


def read_keyframe(store: AssetStore, asset_id: str, index: int, *, analysis_id: str) -> bytes:
    """Validate and read a frame atomically against its analysis cache version."""
    with store._lock, store._process_lock():
        path = _keyframe_path_under_store_lock(store, asset_id, index, analysis_id)
        with path.open("rb") as stream:
            data = stream.read(_KEYFRAME_BYTES_LIMIT + 1)
        if not 3 <= len(data) <= _KEYFRAME_BYTES_LIMIT or data[:3] != b"\xff\xd8\xff":
            raise MediaAnalysisError("keyframe is unavailable")
        return data


def search_assets(store: AssetStore, query: str) -> list[dict[str, Any]]:
    """Search manifest and analysis snapshots while cross-process mutation is excluded."""
    if not isinstance(query, str) or len(query) > 120 or any(ord(char) < 32 for char in query):
        raise MediaAnalysisError("invalid search query")
    needle = " ".join(query.split()).casefold()
    with store._lock, store._process_lock():
        records = store._read_manifest()
        if not needle:
            return [dict(item) for item in records]
        found: list[dict[str, Any]] = []
        for record in records:
            searchable: dict[str, Any] = {
                "filename": record.get("filename"),
                "kind": record.get("kind"),
                "size_bytes": record.get("size_bytes"),
                "metadata": record.get("metadata"),
            }
            if record.get("kind") == "video":
                try:
                    _verify_managed_bytes_under_store_lock(store, record)
                except ValueError:
                    analysis = None
                else:
                    analysis = _load_analysis(store, record["id"], record)
                if analysis:
                    searchable["analysis"] = {
                        "metadata": analysis.get("metadata"),
                        "scenes": analysis.get("scenes"),
                        "scene_detection": analysis.get("scene_detection"),
                    }
            try:
                haystack = json.dumps(searchable, ensure_ascii=False, sort_keys=True).casefold()
            except (TypeError, ValueError):
                continue
            if needle in haystack:
                found.append(dict(record))
        return found


def _select_scene_candidates(
    detections: list[dict[str, Any]], limit: int, duration: float,
) -> list[dict[str, Any]]:
    """Keep strong scene candidates distributed across the analyzed duration."""
    if len(detections) <= limit:
        return detections
    selected: list[dict[str, Any]] = []
    chosen_pts: set[int] = set()
    for bucket in range(limit):
        start = duration * bucket / limit
        end = duration * (bucket + 1) / limit
        candidates = [
            item for item in detections
            if item["pts"] not in chosen_pts
            and start <= item["start_seconds"] < end
        ]
        if candidates:
            choice = max(candidates, key=lambda item: (item["score"], -item["start_seconds"]))
            selected.append(choice)
            chosen_pts.add(choice["pts"])
    if len(selected) < limit:
        remaining = [item for item in detections if item["pts"] not in chosen_pts]
        remaining.sort(key=lambda item: (-item["score"], item["start_seconds"]))
        selected.extend(remaining[:limit - len(selected)])
    return sorted(selected, key=lambda item: item["start_seconds"])


class MediaIntelligence:
    """Analyze an imported video without exposing arbitrary filesystem paths."""

    def __init__(
        self,
        store: AssetStore,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        timeout: float = 120,
    ) -> None:
        if not isinstance(ffmpeg, str) or not ffmpeg or not isinstance(ffprobe, str) or not ffprobe:
            raise ValueError("FFmpeg and FFprobe executable names are required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("analysis timeout must be greater than 0 and at most 120 seconds")
        self.store = store
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.timeout = float(timeout)

    def analyze(
        self,
        asset_id: str,
        *,
        scene_threshold: float = 0.35,
        max_scenes: int = _SCENE_COUNT_LIMIT,
        publication_lock: Any = None,
    ) -> dict[str, Any]:
        if not _ASSET_ID.fullmatch(asset_id):
            raise MediaAnalysisError("invalid asset id")
        if (isinstance(scene_threshold, bool) or not isinstance(scene_threshold, (int, float))
                or not math.isfinite(scene_threshold) or not 0.05 <= scene_threshold <= 0.95):
            raise MediaAnalysisError("scene threshold must be between 0.05 and 0.95")
        if isinstance(max_scenes, bool) or not isinstance(max_scenes, int) or not 1 <= max_scenes <= _SCENE_COUNT_LIMIT:
            raise MediaAnalysisError(f"max scenes must be between 1 and {_SCENE_COUNT_LIMIT}")
        threshold = round(float(scene_threshold), 3)
        deadline = time.monotonic() + self.timeout
        with self.store._lock, self.store._process_lock():
            record, source, probe = self._verified_asset_under_store_lock(asset_id, deadline=deadline)
            cached = _load_analysis(self.store, asset_id, record)
        if (cached and cached.get("scene_detection", {}).get("threshold") == threshold
                and cached.get("scene_detection", {}).get("max_scenes") == max_scenes):
            publication = publication_lock if publication_lock is not None else self.store._lock
            with publication:
                with self.store._lock, self.store._process_lock():
                    record, source, probe = self._verified_asset_under_store_lock(
                        asset_id, deadline=deadline,
                    )
                    cached = _load_analysis(self.store, asset_id, record)
                    if (cached and cached.get("scene_detection", {}).get("threshold") == threshold
                            and cached.get("scene_detection", {}).get("max_scenes") == max_scenes):
                        self._remaining(deadline)
                        return cached

        self._remaining(deadline)
        metadata = self._metadata(record, probe)
        video_stream = next(stream for stream in probe["streams"] if stream.get("codec_type") == "video")
        source_time_origin = _finite_number(video_stream.get("start_time"))
        if source_time_origin is None:
            source_time_origin = _finite_number(probe["format"].get("start_time")) or 0.0
        duration = metadata["duration_seconds"]
        analyzed_duration = min(duration, _ANALYSIS_SECONDS_LIMIT)
        truncated_by_duration = duration > _ANALYSIS_SECONDS_LIMIT
        scene_filter = f"select='gt(scene,{threshold:.3f})',metadata=print:file=-"
        scene_cmd = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
            "-t", f"{analyzed_duration:.6f}", "-i", str(source),
            "-map", "0:v:0", "-vf",
            f"setpts=PTS-STARTPTS,trim=duration={analyzed_duration:.6f},{scene_filter}",
            "-an", "-sn", "-dn", "-f", "null", "-",
        ]
        scene_run = self._run(scene_cmd, "scene detection", deadline=deadline)
        detections = []
        seen_pts: set[int] = set()
        for match in _SCENE_LINE.finditer(scene_run.stdout):
            pts = int(match.group("pts"))
            timestamp = float(match.group("time"))
            score = float(match.group("score"))
            if (pts in seen_pts or not math.isfinite(timestamp) or not math.isfinite(score)
                    or timestamp < 0 or timestamp > analyzed_duration + 0.5):
                continue
            seen_pts.add(pts)
            detections.append({
                "pts": pts,
                "start_seconds": round(timestamp, 6),
                "score": round(min(max(score, 0.0), 1.0), 6),
            })
        selected = _select_scene_candidates(detections, max_scenes, analyzed_duration)
        scene_detection_truncated = len(detections) > max_scenes
        output_count = len(selected) + 1
        analysis_id = uuid.uuid4().hex[:12]
        directory = _derivatives_dir(self.store)
        new_keyframes: list[str] = []
        cache_committed = False
        temporary_dir_context = tempfile.TemporaryDirectory(prefix=f".{asset_id}-analysis-", dir=directory)
        try:
            temporary_dir = temporary_dir_context.name
            pattern = str(Path(temporary_dir) / "frame-%03d.jpg")
            selected_pts = "+".join(f"eq(pts,{item['pts']})" for item in selected)
            selection = "eq(n,0)" + (f"+{selected_pts}" if selected_pts else "")
            frame_filter = (
                f"setpts=PTS-STARTPTS,trim=duration={analyzed_duration:.6f},"
                f"select='{selection}',"
                "scale=640:360:force_original_aspect_ratio=decrease"
            )
            frame_cmd = [
                self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
                "-t", f"{analyzed_duration:.6f}", "-i", str(source),
                "-map", "0:v:0", "-vf", frame_filter, "-an", "-sn", "-dn",
                "-frames:v", str(output_count), "-fps_mode", "vfr", "-q:v", "3", "-threads", "1",
                "-f", "image2", "-start_number", "0", pattern,
            ]
            self._run(frame_cmd, "keyframe extraction", deadline=deadline)
            images = sorted(Path(temporary_dir).glob("frame-*.jpg"))
            if len(images) != output_count:
                raise MediaAnalysisError("keyframe count did not match detected scene boundaries")
            total_image_bytes = 0
            prepared_images: list[tuple[Path, str]] = []
            for index, image in enumerate(images):
                info = image.lstat()
                if (not stat.S_ISREG(info.st_mode) or image.is_symlink() or info.st_nlink != 1
                        or not 3 <= info.st_size <= _KEYFRAME_BYTES_LIMIT):
                    raise MediaAnalysisError("keyframe output is invalid or exceeds its per-frame limit")
                with image.open("rb") as stream:
                    if stream.read(3) != b"\xff\xd8\xff":
                        raise MediaAnalysisError("keyframe output is not a JPEG")
                total_image_bytes += info.st_size
                name = f"{asset_id}-analysis-{analysis_id}-{index:03d}.jpg"
                target = directory / name
                if target.exists() or target.is_symlink():
                    raise MediaAnalysisError("keyframe destination already exists")
                prepared_images.append((image, name))
            if total_image_bytes > _ANALYSIS_OUTPUT_LIMIT:
                raise MediaAnalysisError("analysis keyframes exceeded their total size limit")

            scenes = [{"index": 0, "start_seconds": round(source_time_origin, 6), "score": None}]
            scenes.extend(
                {"index": index + 1, **detection,
                 "start_seconds": round(source_time_origin + detection["start_seconds"], 6)}
                for index, detection in enumerate(selected)
            )
            result = {
                "schema_version": _CACHE_SCHEMA,
                "asset_id": asset_id,
                "analysis_id": analysis_id,
                "source_sha256": record["sha256"],
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
                "tool_versions": {
                    "ffmpeg": self._version(self.ffmpeg, deadline=deadline),
                    "ffprobe": self._version(self.ffprobe, deadline=deadline),
                },
                "metadata": metadata,
                "duration_truncated": truncated_by_duration,
                "analyzed_duration_seconds": round(analyzed_duration, 6),
                "scene_detection": {
                    "algorithm": "FFmpeg select(scene_score)",
                    "threshold": threshold,
                    "max_scenes": max_scenes,
                    "candidate_count": len(detections),
                    "truncated": scene_detection_truncated,
                },
                "scenes": scenes,
                "keyframes": [name for _, name in prepared_images],
            }
            encoded_cache_size = len(_cache_payload(result))
            publication = publication_lock if publication_lock is not None else self.store._lock
            with publication:
                with self.store._lock, self.store._process_lock():
                    self._remaining(deadline)
                    after_record = self.store._asset(asset_id)
                    if (after_record.get("sha256") != record.get("sha256")
                            or after_record.get("path") != record.get("path")):
                        raise MediaAnalysisError("video asset changed during analysis")
                    _verify_managed_bytes_under_store_lock(
                        self.store, after_record, deadline=deadline,
                    )
                    self._remaining(deadline)
                    replacement_names = self._replaceable_analysis_files(directory, asset_id)
                    cache_path = _cache_path(self.store, asset_id)
                    try:
                        cache_info = cache_path.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        if stat.S_ISREG(cache_info.st_mode) and cache_info.st_nlink == 1:
                            replacement_names.add(cache_path.name)
                    self.store._check_derivative_total(
                        replacing=replacement_names,
                        additional_bytes=total_image_bytes + encoded_cache_size,
                    )
                    self._remaining(deadline)
                    for image, name in prepared_images:
                        self._remaining(deadline)
                        os.replace(image, directory / name)
                        new_keyframes.append(name)
                    self._remaining(deadline)
                    self._write_cache(asset_id, result)
                    cache_committed = True
                    self.cleanup_stale_keyframes(asset_id, set(new_keyframes))
            return result
        except BaseException:
            if not cache_committed:
                for name in new_keyframes:
                    (directory / name).unlink(missing_ok=True)
            raise
        finally:
            temporary_dir_context.cleanup()

    @staticmethod
    def _replaceable_analysis_files(directory: Path, asset_id: str) -> set[str]:
        """Find this asset's replaceable regular keyframes, excluding aliases."""
        prefix = f"{asset_id}-analysis-"
        replaceable: set[str] = set()
        for path in directory.glob(f"{prefix}*.jpg"):
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                replaceable.add(path.name)
        return replaceable

    def _verified_asset_under_store_lock(
        self, asset_id: str, *, deadline: float,
    ) -> tuple[dict[str, Any], Path, dict[str, Any]]:
        """Validate bytes and metadata with the caller's shared wall-clock budget."""
        self._remaining(deadline)
        record = self.store._asset(asset_id)
        if record["kind"] != "video":
            raise ValueError("asset must be video")
        source = self.store._managed_path(record)
        _verify_managed_bytes_under_store_lock(self.store, record, deadline=deadline)
        probe = self._probe(source, deadline=deadline)
        self._remaining(deadline)
        if self.store._inspect(source, record["kind"], probe_data=probe) != record["metadata"]:
            raise ValueError("managed asset metadata no longer matches its validated probe")
        self._remaining(deadline)
        return dict(record), source, probe

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MediaAnalysisError("ANALYSIS_DEADLINE_EXCEEDED")
        return remaining

    def _probe(self, source: Path, *, deadline: float) -> dict[str, Any]:
        command = [
            self.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source),
        ]
        try:
            result = _bounded_subprocess(
                command, timeout=min(self._remaining(deadline), 30),
                stdout_limit=4 * 1024 * 1024, stderr_limit=1024 * 1024,
            )
        except _OutputLimitExceeded as exc:
            raise MediaAnalysisError("FFPROBE_FAILED_OR_OUTPUT_LIMIT") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaAnalysisError("FFPROBE_UNAVAILABLE_OR_TIMEOUT") from exc
        if result.returncode:
            raise MediaAnalysisError("FFPROBE_FAILED_OR_OUTPUT_LIMIT")
        try:
            data = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MediaAnalysisError("FFPROBE_RETURNED_INVALID_JSON") from exc
        if (not isinstance(data, dict) or not isinstance(data.get("format"), dict)
                or not isinstance(data.get("streams"), list) or len(data["streams"]) > 64):
            raise MediaAnalysisError("FFPROBE_MEDIA_STRUCTURE_INVALID")
        if not any(isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in data["streams"]):
            raise MediaAnalysisError("VIDEO_STREAM_NOT_FOUND")
        return data

    @staticmethod
    def _metadata(record: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
        fmt = probe["format"]
        streams = probe["streams"]
        duration = _finite_positive(fmt.get("duration"))
        if duration is None:
            video_stream = next((item for item in streams if item.get("codec_type") == "video"), {})
            duration = _finite_positive(video_stream.get("duration"))
        if duration is None:
            raise MediaAnalysisError("VIDEO_DURATION_UNAVAILABLE")
        sanitized_streams = []
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            row: dict[str, Any] = {
                "index": _nonnegative_int(stream.get("index")),
                "type": stream.get("codec_type") if stream.get("codec_type") in {"video", "audio", "subtitle", "data"} else "unknown",
                "codec": _safe_tag(stream.get("codec_name")),
                "profile": _safe_tag(stream.get("profile")),
                "duration_seconds": _finite_positive(stream.get("duration")),
                "bit_rate": _optional_positive_int(stream.get("bit_rate")),
            }
            for source_key, target_key in (("width", "width"), ("height", "height"),
                                           ("sample_rate", "sample_rate"), ("channels", "channels")):
                value = _optional_positive_int(stream.get(source_key))
                if value is not None:
                    row[target_key] = value
            fps = _ratio(stream.get("avg_frame_rate")) or _ratio(stream.get("r_frame_rate"))
            if fps:
                row["fps"] = fps
            for field in ("pix_fmt", "color_space", "color_transfer", "color_primaries"):
                safe = _safe_tag(stream.get(field))
                if safe:
                    row[field] = safe
            sanitized_streams.append(row)
        tags = fmt.get("tags") if isinstance(fmt.get("tags"), dict) else {}
        clean_tags = {key: tag for key in _TAG_KEYS if (tag := _safe_tag(tags.get(key)))}
        return {
            "filename": record["filename"],
            "container": _safe_tag(fmt.get("format_name")),
            "duration_seconds": round(duration, 6),
            "size_bytes": int(record["size_bytes"]),
            "bit_rate": _optional_positive_int(fmt.get("bit_rate")),
            "width": next((row["width"] for row in sanitized_streams if row["type"] == "video" and "width" in row), None),
            "height": next((row["height"] for row in sanitized_streams if row["type"] == "video" and "height" in row), None),
            "streams": sanitized_streams,
            "tags": clean_tags,
        }

    def _run(self, command: list[str], operation: str, *, deadline: float) -> subprocess.CompletedProcess[str]:
        try:
            result = _bounded_subprocess(
                command, timeout=self._remaining(deadline),
                stdout_limit=16 * 1024 * 1024, stderr_limit=1024 * 1024,
            )
        except _OutputLimitExceeded as exc:
            raise MediaAnalysisError(f"{operation.upper().replace(' ', '_')}_OUTPUT_LIMIT") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaAnalysisError(f"{operation.upper().replace(' ', '_')}_FAILED_OR_TIMEOUT") from exc
        if result.returncode:
            raise MediaAnalysisError(f"{operation.upper().replace(' ', '_')}_FAILED")
        return result

    def _version(self, executable: str, *, deadline: float) -> str:
        try:
            remaining = self._remaining(deadline)
            result = _bounded_subprocess(
                [executable, "-version"], timeout=min(5, remaining),
                stdout_limit=64 * 1024, stderr_limit=64 * 1024,
            )
        except (OSError, subprocess.TimeoutExpired, MediaAnalysisError, _OutputLimitExceeded):
            return "unavailable"
        if result.returncode:
            return "unavailable"
        return (result.stdout.splitlines()[0][:160] if result.stdout else "unknown")

    def _write_cache(self, asset_id: str, data: dict[str, Any]) -> None:
        target = _cache_path(self.store, asset_id)
        payload = _cache_payload(data)
        if not payload or len(payload) > _CACHE_BYTES_LIMIT:
            raise MediaAnalysisError("analysis cache exceeded its safety limit")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{asset_id}-analysis-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if target.is_symlink():
                raise MediaAnalysisError("analysis cache must not be a symlink")
            os.replace(temporary_name, target)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def cleanup_stale_keyframes(self, asset_id: str, keep: set[str]) -> None:
        if not _ASSET_ID.fullmatch(asset_id):
            return
        directory = _derivatives_dir(self.store)
        prefix = f"{asset_id}-analysis-"
        for path in directory.glob(f"{prefix}*.jpg"):
            if path.name in keep or path.is_symlink():
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
