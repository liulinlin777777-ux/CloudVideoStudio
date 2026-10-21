# CloudVideoStudio

**Cloud AI Professional Video Studio**

[English](README.md) · [简体中文](README.zh-CN.md)

CloudVideoStudio is an open-source cloud-native AI video editing project.
The current implementation is a **P0 CPU technical prototype**: versioned
project models, exact rational frame rates, a bounded two-video/two-audio
timeline, real FFmpeg rendering, a trusted proxy plugin, and a loopback preview.
AI editing, generation, digital humans, and a professional workbench remain
planned capabilities. This is not a production platform release.

## Run the technical baseline

Use Python 3.12+, FFmpeg/FFprobe with libx264/AAC, and optionally MLT's `melt`.
The evaluated versions and licenses are recorded in [DEPENDENCIES.md](docs/DEPENDENCIES.md).

```sh
python -m pip install -e '.[interchange]'
python -m unittest discover -s tests -v
python tools/media_smoke.py --output-dir build/media
python tools/candidate_smoke.py --output-dir build/candidates
python tools/preview_server.py --media-root build/media
```

Open `http://127.0.0.1:8774` for the synthetic-media preview. The prototype
supports one 640×360, 120-frame topology at 30000/1001 fps: trimmed clips,
crossfade, PIP overlay, two mixed audio tracks, frame-offset editing, project
save/load, and a CPU render receipt. It deliberately rejects unsupported
track topologies and effects. Original source files are preserved.

The independent smoke script decodes the output, checks frame colors and
transition/PIP placement, measures both audio frequencies and their onset
alignment, and runs the real proxy plugin including timeout/error cases.
CI retains public synthetic evidence as downloadable artifacts.

## Project direction

See [ROADMAP.md](ROADMAP.md) for P0–P5. Safari, 4K/GPU, a professional workbench,
and a published Codex Cloud runtime remain unverified. No paid infrastructure
or continuous 24-hour execution is implied.

Community contributions are welcome through Issues, Discussions, and Pull
Requests. Read [CONTRIBUTING.md](CONTRIBUTING.md), the
[Code of Conduct](CODE_OF_CONDUCT.md), and [SECURITY.md](SECURITY.md).
Original project code is [MIT](LICENSE). Third-party tools retain their own
licenses; no FFmpeg/MLT binaries or third-party source are vendored here.
