# P2-A local asset ingest status

## Implemented in this slice

- The existing preview page uses the browser file picker and sends selected file bytes to the loopback server. The server requires an allowed loopback `Host` and exact loopback `Origin`, accepts only a filename (never a client path), and caps a request at 512 MiB. There is no arbitrary file-serving route.
- `AssetStore` copies media without editing the selected original, rejects symlinks (including source and store-root ancestors), hard links, unsupported suffixes, path escapes, invalid UTF-8 SRT and files above configured limits, and records the SHA-256 of the copied bytes with versioned metadata. Store transactions use both in-process and OS file locks, so independently created processes cannot overwrite one another's manifest updates. The manifest reader accepts version 1 and writes version 2.
- MP4/MOV video is checked with FFprobe for an accepted H.264 profile and container. MP3/WAV codecs, JPEG/PNG stream types, and SRT cue/time syntax are checked before the asset is accepted.
- Relink stages replacement bytes under a fresh managed filename and atomically switches the manifest before deleting the old file. A crash before the manifest switch leaves the old record intact; a crash after it leaves the new record intact. Thumbnail and proxy generation uses bounded FFmpeg time, duration and output sizes; derivatives are validated before serving and share a 512 MiB store limit.
- The preview server caps active request handlers at eight and simultaneous browser uploads at two; excess work receives HTTP 503 before reading upload bytes. Uploads remain limited to 512 MiB each, exact loopback origins, and a 2 GiB aggregate source store.
- Project schema 1 serialization remains unchanged and schema-1 files load without being rewritten. `Project.upgrade_asset_metadata()` migrates in-memory asset records to metadata version 2 before changing the schema to 2; schema-2 validation rejects records without this explicit migration marker.
- Video probe metadata includes exact rational FPS when FFprobe provides a valid `avg_frame_rate` or `r_frame_rate`.

## Limits and remaining acceptance

The browser upload limit is 512 MiB per file and 2 GiB total for imported originals. The `AssetStore` API defaults to 2 GiB per file and 10 GiB total for non-browser callers. Thumbnails are capped at 5 MiB each, proxies at 256 MiB each and 600 seconds, and all derivatives together at 512 MiB. FFprobe and FFmpeg subprocesses have explicit time bounds.

Regression coverage includes symlinked ancestors, mutation during source inspection/copy, cross-process manifest writes, and conditional synthetic H.264 import, thumbnail, proxy and full-decode checks when FFmpeg/FFprobe and the H.264 encoder are available. This execution has not run the L3 unit-test command, browser E2E, real-media decode, Mac media acceptance, or Ubuntu CI. Those results are **NOT_VERIFIED** here and must be recorded independently by L3. The current browser library displays imported assets and metadata; timeline placement of arbitrary assets belongs to a later slice. No product or external acceptance PASS is claimed.
