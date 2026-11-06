"""Loopback media gateway — lets ffmpeg read Plex files without ever holding the Plex token.

A command line is world-readable (`ps`, /proc/<pid>/cmdline), so putting the token in
ffmpeg's URL or -headers would hand it to every local user. Instead ffmpeg is pointed at

    http://127.0.0.1:<port>/<signature>/<plex path>

and this server adds the token and forwards the request to Plex. `Range` is passed
through, so ffmpeg can still seek straight to the middle of a file.

What the URL does reveal is limited: the server only listens on loopback, and it forwards a
path only if the signature (HMAC under a key generated at startup) matches, so it cannot be
used as an open Plex proxy. Someone who reads the URL can fetch that one file until the app
restarts — never the token, and nothing else on the server.

The same forwarding core also backs the browser-facing /plex/<kind>/ routes in app.py, which is
why the helpers below (open_plex, BROWSER_PREFIXES, rewrite_playlist) live here.
"""

import hashlib
import hmac
import logging
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)

# Upstream response headers worth relaying (everything ffmpeg needs for sizing and seeking).
_RELAYED_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
_COPY_BYTES = 64 * 1024

# Plex paths the app serves to browsers without a token, keyed by the /plex/<kind>/ URL segment.
BROWSER_PREFIXES = {
    "hls": "/video/:/transcode/universal/",     # transcoded HLS (playlists + segments)
    "part": "/library/parts/",                  # original media files
}
_URI_ATTR = re.compile(r'URI="([^"]*)"')


def open_plex(base_url: str, token: str, plex_path: str, query=(), range_header: str | None = None):
    """Start streaming a GET of a Plex path with the token added. Caller closes the response.

    Any token in `query` is discarded in favour of ours. Raises requests.RequestException;
    never log its text — requests embeds the full URL, token included.
    """
    params = [(k, v) for k, v in query if k.lower() != "x-plex-token"]
    params.append(("X-Plex-Token", token))
    headers = {"Range": range_header} if range_header else {}
    return requests.get(f"{base_url}{quote(plex_path, safe='/:')}", params=params,
                        headers=headers, stream=True, timeout=(5, 30))


def _browser_uri(uri: str, plex_netloc: str) -> str:
    """Make one URI from a Plex playlist routable through /plex/<kind>/ and free of the token."""
    parts = urlsplit(uri)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() != "x-plex-token"])
    path = parts.path
    if parts.scheme or parts.netloc:
        if parts.netloc != plex_netloc:
            return uri                              # someone else's URL — not ours to touch
        for kind, prefix in BROWSER_PREFIXES.items():
            if path.startswith(prefix):
                return urlunsplit(("", "", f"/plex/{kind}/{path[len(prefix):]}", query, ""))
        return urlunsplit((parts.scheme, parts.netloc, path, query, ""))   # at least drop the token
    return urlunsplit(("", "", path, query, ""))


def rewrite_playlist(text: str, plex_base_url: str) -> str:
    """Rewrite an HLS playlist from Plex so a browser can follow it through /plex/<kind>/.

    Plex normally uses relative URIs, which already resolve under the proxied path; this
    covers the cases where it does not: absolute URLs pointing at Plex, and URIs that carry
    the token in their query string.
    """
    netloc = urlsplit(plex_base_url).netloc
    out = []
    for line in text.splitlines():
        if line.startswith("#"):
            line = _URI_ATTR.sub(lambda m: f'URI="{_browser_uri(m.group(1), netloc)}"', line)
        elif line.strip():
            line = _browser_uri(line.strip(), netloc)
        out.append(line)
    return "\n".join(out) + "\n"


class MediaProxy:
    def __init__(self, upstream):
        """upstream() → (base_url, token) for the Plex server, or None if it isn't configured.

        Called per request so a changed token/URL is picked up without a restart.
        """
        self._upstream = upstream
        self._key = secrets.token_bytes(32)
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    def _sign(self, plex_path: str) -> str:
        return hmac.new(self._key, plex_path.encode(), hashlib.sha256).hexdigest()[:32]

    def _ensure_started(self) -> ThreadingHTTPServer:
        with self._lock:
            if self._server is None:
                server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
                server.daemon_threads = True
                server.proxy = self
                threading.Thread(target=server.serve_forever, name="media-proxy",
                                 daemon=True).start()
                self._server = server
                log.info("Media proxy listening on 127.0.0.1:%d", server.server_port)
            return self._server

    def url_for(self, plex_path: str) -> str:
        """Local URL ffmpeg can open for a Plex path such as /library/parts/1/2/file.mkv."""
        port = self._ensure_started().server_port
        return f"http://127.0.0.1:{port}/{self._sign(plex_path)}{quote(plex_path, safe='/')}"

    def verified_path(self, request_path: str) -> str | None:
        """The Plex path a request is asking for, or None if we did not sign it."""
        sig, _, rest = urlsplit(request_path).path.lstrip("/").partition("/")
        plex_path = unquote("/" + rest)
        # Compare as bytes: compare_digest rejects non-ASCII str, and the request line is untrusted.
        if hmac.compare_digest(sig.encode(), self._sign(plex_path).encode()):
            return plex_path
        return None

    def upstream(self) -> tuple | None:
        return self._upstream()


class _Handler(BaseHTTPRequestHandler):
    server_version = "NostalgiaVisionMedia"

    def do_GET(self):
        self._forward(send_body=True)

    def do_HEAD(self):
        self._forward(send_body=False)

    def _forward(self, send_body: bool):
        proxy = self.server.proxy
        plex_path = proxy.verified_path(self.path)
        if plex_path is None:
            return self.send_error(403)
        target = proxy.upstream()
        if not target:
            return self.send_error(503, "Plex is not configured")
        base_url, token = target
        try:
            upstream = open_plex(base_url, token, plex_path,
                                 range_header=self.headers.get("Range"))
        except requests.RequestException as exc:
            log.warning("Media proxy: Plex request failed (%s)", type(exc).__name__)
            return self.send_error(502)

        with upstream:
            self.send_response(upstream.status_code)
            for name in _RELAYED_HEADERS:
                if name in upstream.headers:
                    self.send_header(name, upstream.headers[name])
            self.end_headers()
            if not send_body:
                return
            try:
                for chunk in upstream.raw.stream(_COPY_BYTES, decode_content=False):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass        # ffmpeg went away (seek, program change, viewer left)
            except Exception as exc:        # urllib3 read errors, timeouts, ...
                log.warning("Media proxy: Plex stream broke (%s)", type(exc).__name__)

    def log_message(self, format, *args):
        pass        # per-request lines would only add noise; failures are logged above
