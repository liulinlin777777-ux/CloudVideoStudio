# P2-B bounded timeline implementation notes

## Scope and limits

The P2-B source change keeps the existing fixed P1 2V/2A compiler for its frozen topology and routes other supported topologies to a bounded multi-track FFmpeg filter graph. The project model permits up to four video tracks, four audio tracks and twenty clips. The extensible compiler accepts 25/1 and 30000/1001 fps, up to 1280x720 and at most 60 seconds. It supports black gaps, ordered video compositing, bounded PIP rectangles, volume, and overlaps only when the later clip declares a matching crossfade duration. Unknown effects and invalid crossfade graphs fail closed.

The existing FFmpeg path remains the renderer; no renderer dependency or binary was added. Timeline edits are immutable project transforms for add clip/track, move, trim, split, delete, reorder, and same-kind track moves with optional frame snapping. The page retains numeric editing and adds track creation, media placement, drag movement, a playhead, zoom, split/delete and local undo/redo. Each accepted edit and undo/redo is autosaved through the existing project endpoint; manual save and reload remain available.

## Renderer comparison and acceptance status

This execution did not run unit tests, browser automation, FFmpeg render/decode checks, or resource measurements. The readable P0 acceptance record reports FFmpeg 9.0.2 decoding a 120-frame, 640×360, 30000/1001 fps output (4.004 seconds), and MLT 7.42.0 producing a 60-frame candidate video. Those are historical P0 measurements, not a like-for-like P2-B comparison: the MLT record gives no matching resolution, frame rate, wall/CPU time, output size, or image comparison. This execution did not independently benchmark either renderer or validate distribution licensing. No P2 renderer performance or visual-equivalence result is claimed.

| Capability | Source implementation | Independent evidence in this execution |
| --- | --- | --- |
| P1 fixed 2V/2A | Existing FFmpeg compiler path retained; P0 record: FFmpeg 9.0.2, 120 frames, 640×360, 30000/1001 fps, 4.004 seconds, full decode | Historical P0 evidence only; no new render in this execution |
| Bounded multi-track graph | Added FFmpeg filter-graph path; max 4V/4A, 20 clips, 60 seconds, 1280×720 | Not rendered or decoded here |
| MLT/melt comparison | No integration selected; P0 record: MLT 7.42.0 produced a 60-frame candidate, without comparable resource or image measurements | Not a P2 benchmark; no like-for-like comparison available |
| Existing process bounds | P0 records FFmpeg render timeout ≤120 s, proxy timeout ≤30 s, encoder threads ≤2 | Existing limits; not a P2 resource measurement |
| Chrome, Safari, Ubuntu CI | Not exercised by execution role | Not verified |
| FFmpeg/MLT redistribution licensing | No binaries bundled | Separate release review required |

The L3 workflow must run its frozen exact unit-test command and record validation. Independent browser/media/Ubuntu CI acceptance remains separate; neither internal workflow evidence nor unit-test success alone establishes external product acceptance. A reproducible P2 FFmpeg/MLT comparison still needs the same input, timeline, output dimensions, frame rate, decode check, visual comparison, and wall/CPU/resource measurements on both renderers.
