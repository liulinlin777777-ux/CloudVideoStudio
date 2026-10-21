"""Trusted built-in proxy plugin backed by FFmpeg and FFprobe subprocesses."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import resource
import subprocess
import time
from typing import Any


PLUGIN = {
    "id": "builtin.proxy",
    "version": "1.0.0",
    "capabilities": ["video.proxy"],
}


class PluginError(RuntimeError):
    """An expected proxy plugin execution or validation failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _invoke(command: list[str], timeout: float, stage: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, check=False, shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PluginError("TIMEOUT", f"{stage} exceeded {timeout} seconds") from exc
    except OSError as exc:
        raise PluginError("PROCESS_ERROR", f"could not start {stage}: {exc}") from exc
    if result.returncode != 0:
        raise PluginError("PROCESS_ERROR", f"{stage} failed: {result.stderr.strip()}")
    return result


def run_plugin(
    plugin_id: str,
    source: str | Path,
    target: str | Path,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    timeout: float = 30,
    threads: int = 2,
) -> dict[str, Any]:
    """Run the single trusted proxy plugin and verify its resulting dimensions."""
    if plugin_id != PLUGIN["id"]:
        raise PluginError("UNKNOWN_PLUGIN", f"untrusted or unknown plugin: {plugin_id}")
    if (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        or isinstance(timeout, float) and not math.isfinite(timeout)
        or not 0 < timeout <= 30
    ):
        raise PluginError("INVALID_ARGUMENT", "timeout must be finite and greater than 0 through 30 seconds")
    if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 2:
        raise PluginError("INVALID_ARGUMENT", "threads must be an integer from 1 through 2")
    source_path = Path(source).resolve()
    target_path = Path(target).resolve()
    if not source_path.is_file():
        raise PluginError("INPUT_ERROR", "source file does not exist")
    if source_path == target_path:
        raise PluginError("PATH_ALIAS", "target resolves to the input file")
    if target_path.exists():
        try:
            source_stat, target_stat = source_path.stat(), target_path.stat()
        except OSError as exc:
            raise PluginError("PATH_ERROR", str(exc)) from exc
        if (source_stat.st_dev, source_stat.st_ino) == (target_stat.st_dev, target_stat.st_ino):
            raise PluginError("PATH_ALIAS", "target is a hardlink to the input file")
        raise PluginError("OUTPUT_EXISTS", "target file already exists")

    before_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    command = [
        ffmpeg, "-nostdin", "-n", "-i", str(source_path), "-vf", "scale=320:180",
        "-threads", str(threads), "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(target_path),
    ]
    _invoke(command, float(timeout), "FFmpeg proxy")
    if not target_path.is_file() or target_path.stat().st_size <= 0:
        raise PluginError("OUTPUT_ERROR", "FFmpeg produced an empty output")

    probe_command = [
        ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=width,height", "-of", "json", str(target_path),
    ]
    probe_result = _invoke(probe_command, float(timeout), "FFprobe validation")
    try:
        probe = json.loads(probe_result.stdout)
        streams = probe.get("streams")
        if not isinstance(streams, list) or not streams:
            raise ValueError("no video stream")
        dimensions = streams[0]
        if dimensions.get("width") != 320 or dimensions.get("height") != 180:
            raise ValueError("video dimensions are not 320x180")
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
        raise PluginError("PROBE_ERROR", f"invalid proxy video: {exc}") from exc
    after_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "plugin_id": PLUGIN["id"],
        "version": PLUGIN["version"],
        "capabilities": list(PLUGIN["capabilities"]),
        "input": source_path.name,
        "output": target_path.name,
        "probe": probe,
        "resources": {"threads": threads, "timeout": timeout},
        "wall_seconds": time.monotonic() - started,
        "cpu_seconds": max(
            0.0,
            after_cpu.ru_utime + after_cpu.ru_stime - before_cpu.ru_utime - before_cpu.ru_stime,
        ),
        "output_bytes": target_path.stat().st_size,
    }
