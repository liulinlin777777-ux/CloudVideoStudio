"""Validated, bounded P2-C effect helpers shared by the timeline compiler."""

from __future__ import annotations

import math
import hashlib
import re
from typing import Any

from .project import Clip, Effect, Sequence


def finite(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return float(value)


def integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def safe_text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(ch) < 32 for ch in value):
        raise ValueError("title text must be 1-256 printable characters")
    return value


def title_file_token(clip: Clip) -> str:
    """Return a stable, path-safe token for a renderer-owned UTF-8 text file."""
    digest = hashlib.sha256(clip.id.encode("utf-8")).hexdigest()[:24]
    return f"__CVS_TITLE_{digest}__"


def font_alias(value: Any) -> str:
    """Allow only portable fontconfig family aliases, never user paths."""
    aliases = {"Sans": "Sans", "Serif": "Serif", "Monospace": "Monospace"}
    if not isinstance(value, str) or value not in aliases:
        raise ValueError("font family must be Sans, Serif or Monospace")
    return aliases[value]


def safe_srt_uri(value: Any) -> str:
    if not isinstance(value, str) or not value or value.startswith(('/', '\\')) or '\\' in value:
        raise ValueError("subtitle asset must use a safe project-relative path")
    parts = value.split('/')
    if (len(parts) != 3 or parts[:2] != ['assets', 'files']
            or not re.fullmatch(r'[0-9a-f]{32}(?:-[0-9a-f]{32})?\.srt', parts[2])):
        raise ValueError("subtitle must use a registered managed SRT asset path")
    # Escape FFmpeg filter option delimiters. Render-time root containment is
    # enforced independently for every referenced asset.
    return value.replace('\\', '\\\\').replace(':', r'\:').replace("'", r"\'").replace(',', r'\,').replace(';', r'\;').replace('[', r'\[').replace(']', r'\]').replace('=', r'\=')


def video_effect_filters(clip: Clip, sequence: Sequence, frame_time, asset_uris: dict[str, str]) -> list[str]:
    filters: list[str] = []
    for effect in clip.effects:
        p = effect.parameters
        if effect.kind == 'color':
            if set(p) != {'brightness', 'contrast', 'saturation'}:
                raise ValueError("color requires brightness, contrast and saturation")
            brightness = finite(p['brightness'], 'brightness', -1, 1)
            contrast = finite(p['contrast'], 'contrast', 0, 2)
            saturation = finite(p['saturation'], 'saturation', 0, 3)
            filters.append(f"eq=brightness={brightness:g}:contrast={contrast:g}:saturation={saturation:g}")
        elif effect.kind == 'crop':
            if set(p) != {'x', 'y', 'width', 'height'}:
                raise ValueError("crop requires x, y, width and height")
            x = integer(p['x'], 'crop x', 0, sequence.width - 1)
            y = integer(p['y'], 'crop y', 0, sequence.height - 1)
            width = integer(p['width'], 'crop width', 2, sequence.width)
            height = integer(p['height'], 'crop height', 2, sequence.height)
            if any(value % 2 for value in (x, y, width, height)):
                raise ValueError("crop coordinates and dimensions must be even for 4:2:0 video")
            if x + width > sequence.width or y + height > sequence.height:
                raise ValueError("crop rectangle is outside the sequence")
            filters.append(f"crop={width}:{height}:{x}:{y},scale={sequence.width}:{sequence.height}")
        elif effect.kind == 'title':
            required = {'text', 'x', 'y', 'font_size', 'color', 'start_frame', 'duration_frames'}
            if set(p) not in (required, required | {'font_family'}):
                raise ValueError("title requires text, position, size, color and frame duration")
            safe_text(p['text'])
            family = font_alias(p.get('font_family', 'Sans'))
            x = integer(p['x'], 'title x', 0, sequence.width - 1)
            y = integer(p['y'], 'title y', 0, sequence.height - 1)
            size = integer(p['font_size'], 'title font_size', 8, 96)
            color = p['color']
            if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
                raise ValueError("title color must be a six-digit hex color")
            start = integer(p['start_frame'], 'title start_frame', 0, clip.duration_frames - 1)
            duration = integer(p['duration_frames'], 'title duration_frames', 1, clip.duration_frames - start)
            begin = frame_time(start)
            end = frame_time(start + duration)
            clip_origin = frame_time(clip.start_frame)
            local_time = "t" if clip_origin == "0" else f"t-{clip_origin}"
            filters.append(
                f"drawtext=font='{family}':textfile='{title_file_token(clip)}':expansion=none:x={x}:y={y}:"
                f"fontsize={size}:fontcolor={color}"
                f":enable='gte({local_time}\\,{begin})*lt({local_time}\\,{end})'"
            )
        elif effect.kind == 'subtitle':
            required = {'asset_id', 'start_frame', 'duration_frames', 'position', 'font_size', 'color'}
            if set(p) != required:
                raise ValueError("subtitle requires a safe SRT asset, timing, position and style")
            asset_id = p['asset_id']
            if not isinstance(asset_id, str) or asset_id not in asset_uris:
                raise ValueError("subtitle must reference an existing SRT asset")
            uri = safe_srt_uri(asset_uris[asset_id])
            start = integer(p['start_frame'], 'subtitle start_frame', 0, clip.duration_frames - 1)
            duration = integer(p['duration_frames'], 'subtitle duration_frames', 1, clip.duration_frames - start)
            position = p['position']
            if position not in ('top', 'center', 'bottom'):
                raise ValueError("subtitle position must be top, center or bottom")
            size = integer(p['font_size'], 'subtitle font_size', 8, 72)
            color = p['color']
            if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
                raise ValueError("subtitle color must be a six-digit hex color")
            # ASS Alignment determines the anchor; MarginV is a distance from
            # that edge, so a top caption needs a small margin too.
            margin_v = {'bottom': 24, 'center': 0, 'top': 24}[position]
            begin = frame_time(start)
            end = frame_time(start + duration)
            filters.append(
                f"subtitles=filename='{uri}':charenc=UTF-8:force_style='FontSize={size},PrimaryColour=&H00{color[5:7]}{color[3:5]}{color[1:3]},"
                f"FontName=Arial,Alignment={ {'bottom': 2, 'center': 5, 'top': 8}[position] },MarginV={margin_v}'"
            )
        elif effect.kind not in ('crossfade', 'pip'):
            raise ValueError(f"unsupported video effect: {effect.kind}")
    return filters


def audio_effect_values(clip: Clip, track_effects=()) -> tuple[float, bool, int, int]:
    gain, muted, fade_in, fade_out = 1.0, False, 0, 0
    for effect in (*track_effects, *clip.effects):
        p = effect.parameters
        if effect.kind == 'volume':
            if set(p) != {'gain'}:
                raise ValueError("volume requires gain")
            gain *= finite(p['gain'], 'volume gain', 0, 1)
        elif effect.kind == 'mute':
            if set(p) != {'enabled'} or type(p['enabled']) is not bool:
                raise ValueError("mute requires a boolean enabled value")
            muted = muted or p['enabled']
        elif effect.kind == 'normalize':
            if set(p) != {'enabled'} or type(p['enabled']) is not bool:
                raise ValueError("normalize requires a boolean enabled value")
        elif effect.kind == 'fade':
            if set(p) != {'in_frames', 'out_frames'}:
                raise ValueError("fade requires in_frames and out_frames")
            fade_in = max(fade_in, integer(p['in_frames'], 'fade in_frames', 0, clip.duration_frames))
            fade_out = max(fade_out, integer(p['out_frames'], 'fade out_frames', 0, clip.duration_frames))
            if fade_in + fade_out > clip.duration_frames:
                raise ValueError("audio fades may not overlap")
        elif effect.kind != 'crossfade':
            raise ValueError(f"unsupported audio effect: {effect.kind}")
    return (0.0 if muted else gain), muted, fade_in, fade_out
