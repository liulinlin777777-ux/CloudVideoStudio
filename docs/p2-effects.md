# P2-C post-production slice

The bounded compiler accepts these per-clip effects and applies them to the same filter graph used by preview renders and exports:

| Effect | Parameters | Bounds |
| --- | --- | --- |
| `color` | `brightness`, `contrast`, `saturation` | -1..1, 0..2, 0..3 |
| `crop` | `x`, `y`, `width`, `height` | Even-coordinate rectangle inside the sequence for 4:2:0; output is scaled back to sequence size |
| `title` | `text`, optional `font_family`, `x`, `y`, `font_size`, `color`, `start_frame`, `duration_frames` | Printable text up to 256 characters, `Sans`/`Serif`/`Monospace`, 8..96 px, hex color, clip-local frame range |
| `subtitle` | `asset_id`, `start_frame`, `duration_frames`, `position`, `font_size`, `color` | References a registered `.srt` asset; position is top/center/bottom, size 8..72 px |
| `volume` | `gain` | 0..1 linear gain |
| `mute` | `enabled` | Boolean |
| `fade` | `in_frames`, `out_frames` | Non-overlapping fades within the clip |
| `normalize` | `enabled` | Optional EBU R128 loudness normalization after mixing; fixed target -16 LUFS / -1.5 dBTP / 11 LU LRA |

Existing `pip`, `crossfade`, and `volume` effects remain supported. The browser exposes color, title text/font/color/position/size/frame span, imported SRT captions with position/color/size/frame span controls, crop, PIP sizing/position, volume, mute, and fades. Audio volume, mute, and fade can apply to a selected clip or its whole audio track; track effects are stored in the versioned project representation.

Titles use literal UTF-8 text files owned by the renderer, so punctuation is never parsed as filtergraph syntax. The UI offers only the `Sans`, `Serif`, and `Monospace` fontconfig family aliases; arbitrary font paths are rejected. Title visibility uses a half-open clip-local frame interval (`start <= frame < end`) translated from each clip's timeline PTS. Captions reference an asset ID in the saved project, and render-time containment checks constrain the managed `.srt` file to the media root. SRT cue times are interpreted by libass relative to the clip; a transparent YUVA overlay is trimmed in clip-local time before it is positioned on the timeline, and ASS alignment uses edge-relative caption margins. Crop runs at sequence dimensions before an optional PIP scale, so both effects can compose on the same clip. Audio is mixed at 48 kHz with clip and track gain/fades and one 0.95 peak limiter after mixing with automatic level compensation disabled. Before encoding, a bounded FFmpeg analysis pass attaches `astats` to a derived output of the compiled complex graph, then records measured peak and RMS levels. Silence is represented as JSON `null`, keeping the receipt standards-compliant. Optional loudness normalization uses FFmpeg `loudnorm` followed by explicit resampling back to 48 kHz, and the render receipt includes its measured input loudness fields when FFmpeg emits the measurement report. Original media hashes are checked around rendering.

The supported project limits remain four video and four audio tracks, twenty clips, 60 seconds, 720p, and 25 or 30000/1001 fps.

## Local verification

On 2026-10-29, Chrome exercised local import of seven generated MP4, WAV, PNG, and SRT files; color, title, caption, volume, and fade controls; save; browser close and reopen; and render/playback. The versioned project restored the subtitle asset ID and audio settings after reopening. Four generated thumbnails loaded. The 4.004-second output played to completion. The frozen command `python3.12 -m unittest discover -s tests -v` passed all 100 tests, including the SRT selection restore-order regression.

Independent FFprobe reported H.264 640×360 at 30000/1001 fps with 120 decoded frames and AAC at 48 kHz. Full decode exited successfully. Audio sample alignment passed, clipping and unexpected silence were absent, and decoded start/end RMS measurements confirmed the configured fades. Frame 10 visibly contains both the title and SRT caption. Source SHA-256 values matched before import and after export; output SHA-256 was `ed3304f04148f2fc6d6455f230a1bbf41bca51d21287a2342605c80ca4b8e386`.

The local Homebrew `ffmpeg` binary lacked the `drawtext` filter and failed text rendering with `No such filter: 'drawtext'`. Acceptance used the already installed `ffmpeg-full` 9.0.2 binary, which provides both `drawtext` and `subtitles`; the preview server was started with its existing `--ffmpeg` option. This is an environment configuration requirement, not evidence that arbitrary FFmpeg builds support text effects.

These browser and media checks used locally generated valid test media, not user-provided footage. They do not establish 720p performance, Safari or Ubuntu browser behavior, or the required independent L3 Review. P2-C remains pending formal independent review and those remaining release checks.

Audio QA uses installed EBU R128, astats and silence detectors on the actual mixed signal before encoding, including when normalization is off. The receipt reports measured loudness/peak/RMS, actual sample count versus the exact declared timeline, and silence intervals with warnings. Silence uses JSON null rather than infinity. The sample alignment check does not claim semantic lip-sync or replace final encoded-file decode/QA. Title and caption visibility windows use exact clip-local frame counters. Existing SRT cues use the selected clip local clock and are shifted with its timeline placement.

CJK titles resolve an installed font through fontconfig and verify its reported glyph coverage before rendering; unavailable coverage produces an explicit error instead of silent missing-glyph boxes. Install suitable CJK fonts locally on Linux if absent; no font binaries are bundled or downloaded automatically. Monospace titles may use an installed CJK sans fallback for those glyphs. Font paths are never accepted from project input. Fixed option values remain stable when browser translation changes their visible labels.

Immutable generated preview MP4 responses support a single HTTP byte range (206 with Content-Range and Accept-Ranges); malformed, multiple and unsatisfiable ranges return 416. This permits native browser seeking without relaxing the existing origin, filename or symlink guards.

AAC encoding uses an explicit 192 kb/s target. The completed file is decoded through mature EBU R128/astats/silence detectors as a second audio check; an actual nonnegative sample/true peak rejects the render rather than falsely declaring success. Choose normalization or lower gains if output clipping is detected. Pre-export and encoded measurements remain separate in the receipt, and wall/CPU measurements include the post-encode check. No source/project is rewritten by this check.
