# P0 acceptance record

Contract: [Issue #1 V2](https://github.com/liulinlin777777-ux/CloudVideoStudio/issues/1).

This is a bounded technical baseline, not a released professional editor.

## Independently executed

- 20 Python unit tests: project schema, seven model types, save/load, exact rational rate, alias protection, effect/resource validation.
- macOS FFmpeg 9.0.2: 120 frames, 640×360, 30000/1001 fps, 4.004 s, full video/audio decode. 29 independent checks in [mac-media.json](evidence/mac-media.json).
- Two video tracks: trimmed red/blue crossfade plus green picture-in-picture. Two audio tracks: trimmed 440/880 Hz sine mix. Spectral checks, onset detection and video keyframes verify synchronization rather than file existence.
- Built-in proxy plugin 1.0.0 produced and decoded 320×180 output. Actual invalid-media and subprocess-timeout paths verified.
- MLT 7.42.0 produced a 60-frame candidate video; OTIO 0.18.1 saved and restored a rational-rate timeline. [Candidate evidence](evidence/mac-candidates.json).
- Chrome: change cue to frame 36, save, reload, render, decode and play to end. [Browser evidence](evidence/browser.json), [screenshot](evidence/browser-preview.png).
- Source assets are generated lavfi colors and sine tones; hash checks confirm preservation. No user media is included.

## Failure and repair

The first real render failed audio acceptance: an FFmpeg final `atrim` after delayed/padded audio removed the intended silence. Isolated command experiments reproduced the behavior. Rebasing timestamps using `asetpts=N/SR/TB` before final trim restored the onset. The unchanged independent validator then passed. One L3 sandbox attempt was rejected for out-of-scope Python cache files; the next authorized attempt passed. Rejections remain in the private L3 evidence chain.

## Bounds and open acceptance

Only the documented fixed 2V/2A topology is compiled. Cue starts may move between frames 0–60; gains are bounded. No arbitrary edit graph, untrusted plugin loading, production deployment, cloud environment publication, 4K, GPU, Safari or continuous 24-hour runner is claimed. FFmpeg render timeout is at most 120 s, proxy timeout at most 30 s and encoder threads at most two. CI has a 15-minute limit; media evidence records actual CPU, wall time and disk bytes. There is no hard disk quota or hostile-code sandbox for third-party plugins; only the trusted built-in plugin is accepted.

GitHub CI acceptance must be read from this PR's actual check and artifact, independently of the macOS evidence. GitHub hosted CPU CI is not proof of a published Codex Cloud environment.

## Reproduce

Follow README setup, then run `python -m unittest discover -s tests -v`, `python tools/media_smoke.py --output-dir build/fresh-media`, and `python tools/candidate_smoke.py --output-dir build/fresh-candidates`. Use fresh output directories to preserve generated sources. CI uploads synthetic outputs and JSON evidence for seven days.
