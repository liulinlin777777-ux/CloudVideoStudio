"""FFmpeg-backed source generation and compilation for the bounded demo timeline."""

from __future__ import annotations

from fractions import Fraction
import hashlib
import math
from pathlib import Path
import resource
import subprocess
import time
from typing import Any

from .project import Asset, Clip, Effect, Project, Sequence, Track


def make_demo_project() -> Project:
    """Create the small, deterministic timeline used by the demo and tests."""
    assets = [
        Asset("red", "red.mp4", 150),
        Asset("blue", "blue.mp4", 150),
        Asset("green", "green.mp4", 150),
        Asset("tone440", "tone440.wav", 150),
        Asset("tone880", "tone880.wav", 150),
    ]
    sequence = Sequence(
        id="demo-sequence",
        width=640,
        height=360,
        fps_num=30000,
        fps_den=1001,
        tracks=[
            Track(
                "V1", "video",
                [
                    Clip("red-clip", "red", 0, 75, 15),
                    Clip("blue-clip", "blue", 45, 75, 15,
                         [Effect("crossfade", {"duration_frames": 30})]),
                ],
            ),
            Track(
                "V2", "video",
                [Clip("green-clip", "green", 30, 60, 30,
                      [Effect("pip", {"x": 460, "y": 250, "width": 160, "height": 90})])],
            ),
            Track("A1", "audio", [Clip("tone440-clip", "tone440", 0, 120, 15,
                                         [Effect("volume", {"gain": 0.4})])]),
            Track("A2", "audio", [Clip("tone880-clip", "tone880", 30, 60, 15,
                                         [Effect("volume", {"gain": 0.25})])]),
        ],
    )
    project = Project("demo-project", assets=assets, sequences=[sequence])
    project.validate()
    return project


def _fraction_time(frames: int, sequence: Sequence) -> str:
    value = sequence.frame_time(frames)
    # All demo timeline boundaries are terminating decimals. Keep this exact;
    # do not round a frame rate to an integer or approximate its frame times.
    remainder = value.denominator
    twos = fives = 0
    while remainder % 2 == 0:
        remainder //= 2
        twos += 1
    while remainder % 5 == 0:
        remainder //= 5
        fives += 1
    if remainder != 1:
        raise ValueError("timeline time cannot be represented exactly as an FFmpeg decimal")
    # Decimal scaling through integer arithmetic avoids float formatting drift.
    places = max(twos, fives)
    scaled = value.numerator * (10 ** places) // value.denominator
    whole, fractional = divmod(scaled, 10 ** places)
    if not places:
        return str(whole)
    return f"{whole}.{fractional:0{places}d}".rstrip("0").rstrip(".")


def _effect(clip: Clip, kind: str) -> Effect | None:
    matches = [item for item in clip.effects if item.kind == kind]
    if len(matches) > 1:
        raise ValueError(f"clip {clip.id} has duplicate {kind} effects")
    return matches[0] if matches else None


def _finite_number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return float(value)


def _compile_extensible_timeline(project: Project) -> dict[str, Any]:
    """Compile the bounded P2 multi-track subset; unknown effect graphs fail closed."""
    if len(project.sequences) != 1:
        raise ValueError("exactly one sequence is supported")
    sequence = project.sequences[0]
    if not (1 <= len(sequence.tracks) <= 8):
        raise ValueError("a sequence requires one through eight tracks")
    if (sequence.fps_num, sequence.fps_den) not in ((30000, 1001), (25, 1)):
        raise ValueError("supported frame rates are 30000/1001 and 25/1")
    if sequence.width > 1280 or sequence.height > 720:
        raise ValueError("compile dimensions must not exceed 1280x720")
    all_clips = [clip for track in sequence.tracks for clip in track.clips]
    if not 1 <= len(all_clips) <= 20:
        raise ValueError("a sequence requires one through twenty media clips")
    duration_frames = max(clip.start_frame + clip.duration_frames for clip in all_clips)
    max_duration_frames = (60 * sequence.fps_num + sequence.fps_den - 1) // sequence.fps_den
    if duration_frames > max_duration_frames:
        raise ValueError("timeline duration must not exceed 60 seconds")

    expected = {
        "crossfade": {"duration_frames"},
        "pip": {"x", "y", "width", "height"},
        "volume": {"gain"},
    }
    for track in sequence.tracks:
        if track.kind not in {"video", "audio"}:
            raise ValueError("unsupported track kind")
        ordered = sorted(track.clips, key=lambda item: (item.start_frame, item.id))
        if track.kind == "audio" and any(_effect(clip, "pip") or _effect(clip, "crossfade") for clip in ordered):
            raise ValueError("audio tracks do not support video effects")
        for index, later in enumerate(ordered):
            overlaps = [
                previous for previous in ordered[:index]
                if previous.start_frame + previous.duration_frames > later.start_frame
            ]
            crossfade = _effect(later, "crossfade")
            if overlaps:
                if len(overlaps) != 1 or overlaps[0] is not ordered[index - 1]:
                    raise ValueError("a clip may overlap only its immediate predecessor")
                previous = overlaps[0]
                overlap = previous.start_frame + previous.duration_frames - later.start_frame
                if crossfade is None:
                    raise ValueError("overlapping clips require a crossfade effect on the later clip")
                frames = crossfade.parameters.get("duration_frames")
                if isinstance(frames, bool) or not isinstance(frames, int) or frames != overlap:
                    raise ValueError("crossfade duration_frames must equal the overlap")
                if frames >= min(previous.duration_frames, later.duration_frames):
                    raise ValueError("crossfade must be shorter than either clip")
            elif crossfade is not None:
                raise ValueError("crossfade is only valid on an overlapping clip")
        for clip in ordered:
            kinds = [effect.kind for effect in clip.effects]
            if len(kinds) != len(set(kinds)):
                raise ValueError(f"clip {clip.id} has duplicate effects")
            for effect in clip.effects:
                if effect.kind not in expected or set(effect.parameters) != expected[effect.kind]:
                    raise ValueError(f"unsupported effect on clip {clip.id}: {effect.kind}")
                if track.kind == "audio" and effect.kind != "volume":
                    raise ValueError("audio clips only support volume")
                if track.kind == "video" and effect.kind == "volume":
                    raise ValueError("video clips do not support volume")
            pip = _effect(clip, "pip")
            if pip is not None:
                values = {}
                for key, maximum in (("x", sequence.width), ("y", sequence.height),
                                     ("width", sequence.width), ("height", sequence.height)):
                    value = pip.parameters[key]
                    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                        raise ValueError(f"pip {key} is outside the sequence")
                    values[key] = value
                if (not values["width"] or not values["height"] or
                        values["x"] + values["width"] > sequence.width or
                        values["y"] + values["height"] > sequence.height):
                    raise ValueError("pip rectangle is outside the sequence")
            if track.kind == "audio":
                volume = _effect(clip, "volume")
                if volume is not None:
                    _finite_number(volume.parameters["gain"], "volume gain", 0.0, 1.0)

    inputs = list(dict.fromkeys(clip.asset_id for clip in all_clips))
    indexes = {asset_id: index for index, asset_id in enumerate(inputs)}
    fps = f"{sequence.fps_num}/{sequence.fps_den}"

    def filter_seconds(frames: int) -> str:
        micros = sequence.frame_time(frames) * 1_000_000
        rounded = (2 * micros.numerator + micros.denominator) // (2 * micros.denominator)
        whole, fractional = divmod(rounded, 1_000_000)
        return f"{whole}.{fractional:06d}".rstrip("0").rstrip(".")

    def sample_index(frames: int) -> int:
        samples = sequence.frame_time(frames) * 48000
        return (2 * samples.numerator + samples.denominator) // (2 * samples.denominator)

    graph = [
        f"color=c=black:s={sequence.width}x{sequence.height}:r={fps},"
        f"trim=end_frame={duration_frames},setpts=PTS-STARTPTS,format=yuv420p[vbase0]"
    ]
    base = "vbase0"
    node = 1
    video_tracks = [track for track in sequence.tracks if track.kind == "video"]
    for track_index, track in enumerate(video_tracks):
        for clip_index, clip in enumerate(sorted(track.clips, key=lambda item: (item.start_frame, item.id))):
            label = f"vclip{track_index}_{clip_index}"
            start_time = filter_seconds(clip.start_frame)
            pip = _effect(clip, "pip")
            out_width = pip.parameters["width"] if pip else sequence.width
            out_height = pip.parameters["height"] if pip else sequence.height
            graph.append(
                f"[{indexes[clip.asset_id]}:v]fps={fps},"
                f"trim=start_frame={clip.source_in_frame}:"
                f"end_frame={clip.source_in_frame + clip.duration_frames},"
                f"setpts=N*{sequence.fps_den}/{sequence.fps_num}/TB+"
                f"{clip.start_frame}*{sequence.fps_den}/{sequence.fps_num}/TB,"
                f"scale={out_width}:{out_height},setsar=1,fps={fps},"
                f"format=yuva420p[{label}]"
            )
            fade = _effect(clip, "crossfade")
            if fade is not None:
                seconds = filter_seconds(fade.parameters["duration_frames"])
                faded = label + "fade"
                graph.append(f"[{label}]fade=t=in:st={start_time}:d={seconds}:alpha=1[{faded}]")
                label = faded
            if pip is None:
                x, y = 0, 0
            else:
                x, y = pip.parameters["x"], pip.parameters["y"]
            out = f"vbase{node}"
            graph.append(f"[{base}][{label}]overlay=x={x}:y={y}:eof_action=pass:shortest=0:format=auto[{out}]")
            base = out
            node += 1
    if base != "vout":
        graph.append(f"[{base}]null[vout]")

    audio_labels = []
    audio_index = 0
    for track in sequence.tracks:
        if track.kind != "audio":
            continue
        for clip in sorted(track.clips, key=lambda item: (item.start_frame, item.id)):
            label = f"aclip{audio_index}"
            start_sample = sample_index(clip.source_in_frame)
            end_sample = sample_index(clip.source_in_frame + clip.duration_frames)
            delay = sample_index(clip.start_frame)
            volume = _effect(clip, "volume")
            gain = _finite_number(volume.parameters["gain"], "volume gain", 0.0, 1.0) if volume else 1.0
            graph.append(
                f"[{indexes[clip.asset_id]}:a]aresample=48000,atrim=start_sample={start_sample}:"
                f"end_sample={end_sample},asetpts=N/SR/TB,volume={gain:g},adelay={delay}S|{delay}S,"
                f"apad,atrim=end_sample={sample_index(duration_frames)},"
                f"asetpts=N/SR/TB[{label}]"
            )
            audio_labels.append(label)
            audio_index += 1
    if audio_labels:
        graph.append("".join(f"[{label}]" for label in audio_labels) +
                     f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0,"
                     f"atrim=end_sample={sample_index(duration_frames)},"
                     "asetpts=N/SR/TB[aout]")
    else:
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=end_sample={sample_index(duration_frames)}[aout]")
    return {
        "inputs": inputs, "filtergraph": ";".join(graph),
        "duration_frames": duration_frames, "fps": fps,
        "width": sequence.width, "height": sequence.height,
    }


def compile_timeline(project: Project) -> dict[str, Any]:
    """Compile the explicitly supported two-video/two-audio demo topology."""
    project.validate()
    if len(project.sequences) == 1:
        tracks = project.sequences[0].tracks
        if len(tracks) != 4 or [track.kind for track in tracks] != ["video", "video", "audio", "audio"] or [len(track.clips) for track in tracks] != [2, 1, 1, 1]:
            return _compile_extensible_timeline(project)
    if len(project.sequences) != 1:
        raise ValueError("exactly one sequence is supported")
    sequence = project.sequences[0]
    if len(sequence.tracks) != 4 or [track.kind for track in sequence.tracks] != [
        "video", "video", "audio", "audio"
    ]:
        raise ValueError("supported topology is two video tracks followed by two audio tracks")
    v1, v2, a1, a2 = sequence.tracks
    if len(v1.clips) != 2 or len(v2.clips) != 1 or len(a1.clips) != 1 or len(a2.clips) != 1:
        raise ValueError("supported topology requires video clips 2+1 and audio clips 1+1")
    if (sequence.fps_num, sequence.fps_den) not in ((30000, 1001), (25, 1)):
        raise ValueError("supported frame rates are 30000/1001 and 25/1")
    if sequence.width > 1280 or sequence.height > 720:
        raise ValueError("compile dimensions must not exceed 1280x720")

    primary = sorted(v1.clips, key=lambda clip: (clip.start_frame, clip.id))
    first, later = primary
    pip_clip = v2.clips[0]
    audio_clips = [a1.clips[0], a2.clips[0]]
    if first.start_frame == later.start_frame:
        raise ValueError("primary video clips cannot have coincident starts")
    overlap = first.start_frame + first.duration_frames - later.start_frame
    if overlap >= min(first.duration_frames, later.duration_frames):
        raise ValueError("primary video clips cannot fully contain one another")

    expected = {"crossfade": {"duration_frames"}, "pip": {"x", "y", "width", "height"}, "volume": {"gain"}}
    for track in sequence.tracks:
        for clip in track.clips:
            for effect in clip.effects:
                if effect.kind not in expected or set(effect.parameters) != expected[effect.kind]:
                    raise ValueError(f"unsupported effect on clip {clip.id}: {effect.kind}")
    if first.effects:
        raise ValueError("the first primary video clip cannot carry effects")
    first_crossfade = _effect(later, "crossfade")
    if overlap > 0:
        if first_crossfade is None:
            raise ValueError("an overlapping primary clip requires a crossfade")
        value = first_crossfade.parameters["duration_frames"]
        if isinstance(value, bool) or not isinstance(value, int) or value != overlap:
            raise ValueError("crossfade duration_frames must equal the clip overlap")
        if value <= 0 or value >= min(first.duration_frames, later.duration_frames):
            raise ValueError("crossfade duration must be positive and shorter than either clip")
    elif first_crossfade is not None:
        raise ValueError("a crossfade is only valid for overlapping primary clips")
    if any(effect.kind != "crossfade" for effect in later.effects):
        raise ValueError("only the later primary clip may carry a crossfade")

    pip = _effect(pip_clip, "pip")
    if pip is None:
        raise ValueError("the picture-in-picture clip requires a pip effect")
    if len(pip_clip.effects) != 1:
        raise ValueError("the picture-in-picture clip only supports one pip effect")
    coords = {}
    for key, maximum in (("x", sequence.width), ("y", sequence.height), ("width", sequence.width), ("height", sequence.height)):
        value = pip.parameters[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
            raise ValueError(f"pip {key} is outside the sequence")
        coords[key] = value
    if coords["width"] == 0 or coords["height"] == 0 or coords["x"] + coords["width"] > sequence.width or coords["y"] + coords["height"] > sequence.height:
        raise ValueError("pip rectangle is outside the sequence")

    gains = []
    for clip in audio_clips:
        volume = _effect(clip, "volume")
        if volume is None:
            raise ValueError("each audio clip requires a volume effect")
        if len(clip.effects) != 1:
            raise ValueError("audio clips only support one volume effect")
        gains.append(_finite_number(volume.parameters["gain"], "volume gain", 0.0, 1.0))

    used = [first.asset_id, later.asset_id, pip_clip.asset_id, audio_clips[0].asset_id, audio_clips[1].asset_id]
    if len(set(used)) != 5:
        raise ValueError("demo timeline requires five distinct input assets")

    duration_frames = max(
        clip.start_frame + clip.duration_frames
        for track in sequence.tracks for clip in track.clips
    )
    if duration_frames > 300:
        raise ValueError("timeline duration must not exceed 300 frames")
    fps = f"{sequence.fps_num}/{sequence.fps_den}"
    graph = [
        f"color=c=black:s={sequence.width}x{sequence.height}:r={fps},trim=end_frame={duration_frames},setpts=PTS-STARTPTS,format=yuv420p[vblack]"
    ]
    if overlap > 0:
        graph.append("[vblack]split=2[vblackfirst][vblackprimary]")
        first_black, primary_black = "vblackfirst", "vblackprimary"
    else:
        first_black = "vblack"
    input_index = {asset_id: index for index, asset_id in enumerate(used)}

    def video_stream(clip: Clip, label: str, *, size: str, timeline_start: bool = True) -> str:
        index = input_index[clip.asset_id]
        start = clip.start_frame if timeline_start else 0
        graph.append(
            f"[{index}:v]trim=start_frame={clip.source_in_frame}:end_frame={clip.source_in_frame + clip.duration_frames},"
            f"setpts=PTS-STARTPTS+{start}*{sequence.fps_den}/{sequence.fps_num}/TB,"
            f"scale={size},setsar=1,fps={fps},format=yuv420p[{label}]"
        )
        return label

    def filter_time(frames: int) -> str:
        # FFmpeg duration options have microsecond precision. Convert the exact
        # frame Fraction at this boundary; the supported frame periods are far
        # larger than the resulting sub-microsecond representation error.
        value = sequence.frame_time(frames) * 1_000_000
        micros = (2 * value.numerator + value.denominator) // (2 * value.denominator)
        whole, fractional = divmod(micros, 1_000_000)
        return f"{whole}.{fractional:06d}".rstrip("0").rstrip(".")

    first_label = video_stream(first, "vfirst", size=f"{sequence.width}:{sequence.height}")
    graph.append(f"[{first_black}][{first_label}]overlay=eof_action=pass:shortest=0:format=auto[vfirstlayer]")
    if overlap > 0:
        later_label = video_stream(
            later, "vlater", size=f"{sequence.width}:{sequence.height}", timeline_start=False
        )
        graph.append(
            f"[vfirstlayer][{later_label}]xfade=transition=fade:"
            f"duration={filter_time(overlap)}:offset={filter_time(later.start_frame)}[vprimary]"
        )
        graph.append(f"[{primary_black}][vprimary]overlay=eof_action=pass:shortest=0:format=auto[vbase]")
    else:
        later_label = video_stream(later, "vlater", size=f"{sequence.width}:{sequence.height}")
        graph.append(
            f"[vfirstlayer][{later_label}]overlay=eof_action=pass:shortest=0:format=auto"
            f"[vbase]"
        )

    pip_label = video_stream(pip_clip, "vpip", size=f"{coords['width']}:{coords['height']}")
    graph.append(
        f"[vbase][{pip_label}]overlay=x={coords['x']}:y={coords['y']}:eof_action=pass:shortest=0:"
        f"format=auto[vout]"
    )
    for index, clip in enumerate(audio_clips):
        source_index = input_index[clip.asset_id]
        delay_samples = Fraction(
            clip.start_frame * sequence.fps_den * 48000, sequence.fps_num
        )
        delay_samples = (2 * delay_samples.numerator + delay_samples.denominator) // (
            2 * delay_samples.denominator
        )
        source_start = Fraction(clip.source_in_frame * sequence.fps_den * 48000, sequence.fps_num)
        source_start = (2 * source_start.numerator + source_start.denominator) // (2 * source_start.denominator)
        source_end = Fraction((clip.source_in_frame + clip.duration_frames) * sequence.fps_den * 48000, sequence.fps_num)
        source_end = (2 * source_end.numerator + source_end.denominator) // (2 * source_end.denominator)
        graph.append(
            f"[{source_index}:a]aresample=48000,atrim=start_sample={source_start}:end_sample={source_end},"
            f"asetpts=N/SR/TB,volume={gains[index]:g},adelay={delay_samples}S|{delay_samples}S,"
            f"apad,atrim=end_sample={round(Fraction(duration_frames * sequence.fps_den * 48000, sequence.fps_num))},"
            f"asetpts=N/SR/TB[a{index}]"
        )
    graph.extend([
        "[a0][a1]amix=inputs=2:duration=longest:normalize=0,"
        f"atrim=end_sample={round(Fraction(duration_frames * sequence.fps_den * 48000, sequence.fps_num))},"
        "asetpts=N/SR/TB[aout]",
    ])
    return {
        "inputs": used,
        "filtergraph": ";".join(graph),
        "duration_frames": duration_frames,
        "fps": fps,
        "width": sequence.width,
        "height": sequence.height,
    }


def generate_sources(root: str | Path, ffmpeg: str = "ffmpeg") -> dict[str, Path]:
    """Create the five deterministic lavfi demo inputs, refusing replacements."""
    directory = Path(root).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    targets = {
        "red": directory / "red.mp4",
        "blue": directory / "blue.mp4",
        "green": directory / "green.mp4",
        "tone440": directory / "tone440.wav",
        "tone880": directory / "tone880.wav",
    }
    existing = [path for path in targets.values() if path.exists()]
    if existing:
        raise FileExistsError(f"source target already exists: {existing[0].name}")
    colors = {"red": "red", "blue": "blue", "green": "green"}
    for asset_id, color in colors.items():
        command = [
            ffmpeg, "-nostdin", "-n", "-f", "lavfi", "-i",
            f"color=c={color}:s=640x360:r=30000/1001:d=5.005",
            "-frames:v", "150", "-an", "-c:v", "libx264", "-preset", "fast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-threads", "2", str(targets[asset_id]),
        ]
        _run_checked(command, timeout=30, operation=f"generate {asset_id}")
    for asset_id, frequency in (("tone440", 440), ("tone880", 880)):
        command = [
            ffmpeg, "-nostdin", "-n", "-f", "lavfi", "-i",
            f"sine=frequency={frequency}:sample_rate=48000:duration=5.005",
            "-c:a", "pcm_s16le", "-threads", "2", str(targets[asset_id]),
        ]
        _run_checked(command, timeout=30, operation=f"generate {asset_id}")
    return targets


def _run_checked(command: list[str], timeout: float | None, operation: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{operation} timed out") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{operation} failed: {result.stderr.strip()}")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside_root(path: Path, root: Path, name: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{name} must stay inside the render root") from exc
    return resolved


def render_project(
    project: Project,
    root: str | Path,
    output: str | Path,
    ffmpeg: str = "ffmpeg",
    timeout: float = 120,
) -> dict[str, Any]:
    """Render a project to a new file inside root and verify inputs stayed intact."""
    if (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        or isinstance(timeout, float) and not math.isfinite(timeout)
        or not 0 < timeout <= 120
    ):
        raise ValueError("render timeout must be finite and greater than 0 through 120 seconds")
    compiled = compile_timeline(project)
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise ValueError("render root must be an existing directory")
    assets = {asset.id: asset for asset in project.assets}
    source_paths: dict[str, Path] = {}
    for asset_id in compiled["inputs"]:
        uri = Path(assets[asset_id].uri)
        if uri.is_absolute():
            raise ValueError("asset URIs must be relative to the render root")
        source = _inside_root(root_path / uri, root_path, "asset source")
        if not source.is_file():
            raise ValueError(f"asset source does not exist: {assets[asset_id].uri}")
        source_paths[asset_id] = source
    target = Path(output)
    if not target.is_absolute():
        target = root_path / target
    target = _inside_root(target, root_path, "render output")
    for source in source_paths.values():
        if source == target:
            raise ValueError("render output aliases a source asset")
        if target.exists():
            try:
                source_stat, target_stat = source.stat(), target.stat()
            except FileNotFoundError:
                continue
            if (source_stat.st_dev, source_stat.st_ino) == (target_stat.st_dev, target_stat.st_ino):
                raise ValueError("render output hardlinks a source asset")
    if target.exists():
        raise FileExistsError("render output already exists")

    hashes_before = {asset_id: _sha256(path) for asset_id, path in source_paths.items()}
    command = [ffmpeg, "-nostdin", "-n"]
    for asset_id in compiled["inputs"]:
        command.extend(["-i", str(source_paths[asset_id])])
    command.extend([
        "-filter_complex_threads", "1", "-filter_complex", compiled["filtergraph"],
        "-map", "[vout]", "-map", "[aout]", "-r", compiled["fps"],
        "-frames:v", str(compiled["duration_frames"]), "-threads", "2",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-movflags", "+faststart", str(target),
    ])
    before_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg render timed out") from exc
    wall_seconds = time.monotonic() - started
    after_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg render failed: {result.stderr.strip()}")
    if not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError("FFmpeg produced an empty output")
    hashes_after = {asset_id: _sha256(path) for asset_id, path in source_paths.items()}
    if hashes_after != hashes_before:
        raise RuntimeError("FFmpeg modified one or more source assets")
    cpu_seconds = (
        after_cpu.ru_utime + after_cpu.ru_stime - before_cpu.ru_utime - before_cpu.ru_stime
    )
    return {
        "status": "completed",
        "wall_seconds": wall_seconds,
        "cpu_seconds": max(0.0, cpu_seconds),
        "output_bytes": target.stat().st_size,
        "source_hashes_before": hashes_before,
        "source_hashes_after": hashes_after,
    }
