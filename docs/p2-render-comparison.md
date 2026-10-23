# P2-B bounded timeline implementation notes

## Scope and limits

The P2-B source change keeps the existing fixed P1 2V/2A compiler for its frozen topology and routes other supported topologies to a bounded multi-track FFmpeg filter graph. The project model permits up to four video tracks, four audio tracks and twenty clips. The extensible compiler accepts 25/1 and 30000/1001 fps, up to 1280x720 and at most 60 seconds. It supports black gaps, ordered video compositing, bounded PIP rectangles, volume, and overlaps only when the later clip declares a matching crossfade duration. Unknown effects and invalid crossfade graphs fail closed.

The existing FFmpeg path remains the renderer; no renderer dependency or binary was added. Timeline edits are immutable project transforms for add clip/track, move, trim, split, delete, reorder, and same-kind track moves with optional frame snapping. The page retains numeric editing and adds track creation, media placement, drag movement, a playhead, zoom, split/delete and local undo/redo. Each accepted edit and undo/redo is autosaved through the existing project endpoint; manual save and reload remain available.

## Renderer comparison and acceptance status

This execution did not run unit tests, browser automation, FFmpeg render/decode checks, or resource measurements. It also did not independently benchmark MLT/melt or validate distribution licensing. Therefore, this implementation note records the intended compatibility boundary only; it does not claim that FFmpeg or MLT examples were rendered or accepted.

| Capability | Source implementation | Independent evidence in this execution |
| --- | --- | --- |
| P1 fixed 2V/2A | Existing FFmpeg compiler path retained | Not run here |
| Bounded multi-track graph | Added FFmpeg filter-graph path; max 4V/4A, 20 clips, 60 seconds | Not rendered or decoded here |
| MLT/melt comparison | No integration selected | Not benchmarked |
| Chrome, Safari, Ubuntu CI | Not exercised by execution role | Not verified |
| FFmpeg/MLT redistribution licensing | No binaries bundled | Separate release review required |

The L3 workflow must run its frozen exact unit-test command and record validation. Independent browser/media/Ubuntu CI acceptance remains separate; neither internal workflow evidence nor unit-test success alone establishes external product acceptance.
