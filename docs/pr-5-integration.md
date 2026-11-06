# PR #5 integration review

Reviewed against main `5a60f03bd56cf2bdb9991edaeeb43cf6d3b3d5f5`.
Contributor commit `38adf92e5f5f70c018baa33f4ceaf633bfa892df` is preserved as a
merge parent, including its original author and committer.

## Confirmed issues addressed

- Removed runtime config, both SQLite databases, uploaded channel artwork and
  compiled Python caches from tracking. Sanitized config/environment examples
  remain. Git and Docker ignore runtime state. Existing Git history is retained.
- Replaced the hard-coded Docker host volume with `DATA_PATH` (default `./data`).
  `NOSTALGIAVISION_DATA` controls database/logo storage for native Python or a
  custom container layout; config has its own `NOSTALGIAVISION_CONFIG` override.
- Kept activity requests, displayed feed addresses and browser HLS playback under
  the document's proxy subpath. HLS absolute/root-relative media links now use
  the WSGI mount prefix or configured public URL prefix. Feed artwork uses the
  same public base as M3U/XMLTV. Prefix-stripping proxies require `PUBLIC_URL`.
- Rejected nested encoded traversal, backslashes and non-media proxy paths.
  Thumbnail requests are limited to Plex metadata artwork. Upstream redirects
  are not followed; redirect locations and upstream error bodies are not exposed.
  Tokens are stripped from rewritten HLS URIs, including unexpected CDN URLs.
- Started idle cleanup after the final slow subscriber is evicted, removed
  stopped relays from the registry and waited for force-killed FFmpeg processes.
- Replaced a brittle static-slate byte-count expectation with a multiple-chunk
  check while retaining codec verification: static imagery bitrate varies
  between FFmpeg versions.

## Verification

- Full unittest discovery, including contributor and existing tests, plus new
  integration regressions. Media tests run with FFmpeg/ffprobe present.
- Docker image built locally using the supplied Dockerfile. Full tests also run
  as its unprivileged application user, with no external network, no published
  ports, read-only application files and disposable runtime storage.
- Generated clips exercise a scheduled program boundary with increasing video
  DTS, mid-program seeking through a token-protected fake Plex, late joining with
  an actual decoded video frame, multiple viewers sharing a relay, idle FFmpeg
  termination and real HTTP-client disconnect cleanup.
- Mounted `/ntv` and prefix-stripping proxy cases cover feed, artwork and HLS
  routing. Proxy tests cover byte ranges, missing credentials, signed URLs,
  forbidden paths, redirect handling and token-free client responses/processes.
- Container entrypoint smoke test checks `/`, status, M3U and XMLTV, UID 1000,
  custom config/data paths and persistence into an isolated host directory.
- Compose config validation and complete staged-diff review before publication.

## Remaining limits and optional enhancements

- No real Plex/Jellyfin server, actual tuner import, browser HLS session or remote
  reverse proxy was used. Synthetic transition tests do not establish seamless
  playback for all clients, formats, missing audio tracks or slow upstream media.
- Encoding remains software H.264/AAC. Hardware acceleration, broader codec
  coverage, load benchmarks and transition latency tuning are optional follow-up
  work; no fixed CPU-per-channel or transition-delay guarantee is made.
- Feed/UI/API authentication is not implemented. Trusted-network access only.
  Adding authentication is separate work, not a claim that token proxying makes
  this safe to publish openly.
- Plex remains the source; Jellyfin is an IPTV consumer. No alternate/private
  Jellyfin source backend or site-specific configuration was added.
- No production deployment or changes to existing services were performed.
