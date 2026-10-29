# P3-03: local video media intelligence

P3-03 adds an opt-in analysis action to each imported video in the local media
library. It reads only the asset already copied into the project's managed
asset store; it does not accept a path from the browser and does not send media
to a remote service.

## What analysis returns

- FFprobe container, duration, file size, bit rate, video/audio stream codecs,
  stream dimensions, rational frame rate, pixel format, color metadata, audio
  sample rate and channel count, and a small allowlist of descriptive tags.
- FFmpeg scene-score boundary candidates with timestamp and score. The first
  frame is also included as a navigation point.
- JPEG keyframes extracted from the decoded boundary frames at up to 640x360.
- A local metadata search over filenames, probe fields, and saved scene times.

The library reuses the existing FFmpeg/FFprobe install, managed asset hashes,
loopback host/origin checks, and derivative-store limits. No Python package or
new media engine is added. The result cache records the source SHA-256 and is
ignored when a relink changes that digest. Keyframe reads resolve through the
validated per-asset analysis index, not a browser-provided path.

## Limits and status

Each operation is bounded to 120 seconds, examines at most the first 600 seconds
of a source, accepts scene thresholds from 0.05 through 0.95, records up to 24
scene boundaries selected across the analyzed duration plus the opening frame,
caps each keyframe at 2 MiB and total analysis keyframes at 80 MiB. Results
report whether the duration or scene list was truncated.

Scene scores are candidate boundaries, not a semantic description of what a
shot depicts. P3-03 does not generate captions or transcript text. `whisper-cli`
1.9.5 is installed locally, but no model weights were found in the checked local
model/cache locations. No model was downloaded and speech recognition is not
presented as implemented.

The source application remains MIT. FFmpeg/FFprobe are external executables
and retain their own licenses as recorded in [DEPENDENCIES.md](DEPENDENCIES.md);
no third-party code or binary is vendored by this change.
