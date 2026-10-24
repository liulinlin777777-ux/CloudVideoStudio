# P2-C post-production slice

The bounded compiler accepts these per-clip effects and applies them to the same filter graph used by preview renders and exports:

| Effect | Parameters | Bounds |
| --- | --- | --- |
| `color` | `brightness`, `contrast`, `saturation` | -1..1, 0..2, 0..3 |
| `crop` | `x`, `y`, `width`, `height` | Even-coordinate rectangle inside the sequence for 4:2:0; output is scaled back to sequence size |
| `title` | `text`, `x`, `y`, `font_size`, `color`, `start_frame`, `duration_frames` | Printable text up to 256 characters, 8..96 px, hex color, clip-local frame range |
| `subtitle` | `asset_id`, `start_frame`, `duration_frames`, `position`, `font_size`, `color` | References a registered `.srt` asset; position is top/center/bottom, size 8..72 px |
| `volume` | `gain` | 0..1 linear gain |
| `mute` | `enabled` | Boolean |
| `fade` | `in_frames`, `out_frames` | Non-overlapping fades within the clip |
| `normalize` | `enabled` | Optional EBU R128 loudness normalization after mixing; fixed target -16 LUFS / -1.5 dBTP / 11 LU LRA |

Existing `pip`, `crossfade`, and `volume` effects remain supported. The browser exposes color, title, imported SRT captions, crop, PIP sizing/position, volume, mute, and fades. Audio volume, mute, and fade can apply to a selected clip or its whole audio track; track effects are stored in the versioned project representation.

Titles use the fixed `Sans` font family, literal text expansion, and do not accept font names or font paths. Captions reference an asset ID in the saved project, and render-time containment checks constrain the managed `.srt` file to the media root. SRT cue times are interpreted by libass; a timed overlay bounds the selected caption effect interval without passing unsupported `enable` syntax to the subtitles filter. Crop runs at sequence dimensions before an optional PIP scale, so both effects can compose on the same clip. Audio is mixed at 48 kHz with clip and track gain/fades and a 0.95 peak limiter after mixing. Optional loudness normalization uses FFmpeg `loudnorm`; the render receipt includes its measured input loudness fields when FFmpeg emits the measurement report. Original media hashes are checked around rendering.

The supported project limits remain four video and four audio tracks, twenty clips, 60 seconds, 720p, and 25 or 30000/1001 fps. This document describes implemented behavior only. No browser, real-media, full-decode, Mac/Ubuntu CI, or independent P2-C frame/audio acceptance evidence is recorded here; P2-C and product acceptance remain unverified until those checks run independently.
