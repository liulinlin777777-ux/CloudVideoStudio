# P0 dependency decision

The application source is MIT. External media tools and interchange libraries
retain their own licenses; their code and binaries are not vendored here.

| Candidate | Fixed Mac evaluation version | License boundary | Role |
| --- | --- | --- | --- |
| FFmpeg / FFprobe | 9.0.2, Homebrew formula revision 1 | This build enables GPL/version3: GPL-3.0-or-later; upstream base includes LGPL code | External CPU render, probe, decode |
| MLT | 7.42.0 | Framework LGPL-2.1; melt CLI GPL; individual modules may differ | Evaluated native timeline engine candidate |
| OpenTimelineIO | 0.18.1 | Apache-2.0 | Editorial interchange candidate; not a renderer |

Sources: [FFmpeg legal](https://ffmpeg.org/legal.html),
[MLT source and licenses](https://github.com/mltframework/mlt),
[MLT copyright policy](https://github.com/mltframework/mlt_web/blob/master/CopyrightPolicy.md),
[OTIO source](https://github.com/AcademySoftwareFoundation/OpenTimelineIO).

Reproduction on the evaluated Mac uses Homebrew `ffmpeg` and `mlt`, with exact
versions above checked by the evidence scripts; OTIO is pinned in pyproject.toml.
Homebrew formula updates can change versions: verify tool versions before
comparing results. CI uses the Ubuntu 24.04 distribution packages and records
actual versions in its artifacts; it does not assert binary identity with Mac.

P0 uses FFmpeg's existing filtergraph engine through argv-only subprocess calls.
It supports one explicitly bounded two-video/two-audio topology. MLT remains a
candidate for richer P1 timeline integration; its framework and melt CLI are
separately evaluated by `tools/candidate_smoke.py`. This choice avoids adding
bindings, Qt integration, or another rendering abstraction before that work has
a scoped contract. No conclusion that MLT is generally unsuitable is implied.

No FFmpeg/MLT code or binaries are relicensed as MIT. Binary redistribution,
codec patent obligations, new plugins, or library linking require another
license review before release. P0 does not distribute a bundled media runtime.
