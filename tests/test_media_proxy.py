"""The Plex token must never leave the server.

- ffmpeg: a command line is world-readable (`ps`, /proc/<pid>/cmdline), so it reads media
  through a loopback gateway (media_proxy.py) that adds the token itself.
- Browsers: playback goes through /plex/<kind>/... routes that add the token server-side,
  so no URL, page or JSON response the browser sees contains it.
"""

import http.client
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import app
import database as db
import media_proxy
import streamer
from plex_client import PlexClient

TOKEN = "plex-secret-token"
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class FakePlex:
    """Minimal Plex file server: token query param required, Range supported."""

    def __init__(self, files: dict, token: str = TOKEN):
        self.files = files
        self.token = token
        self.requests = []          # (path, token_seen, range_header)
        self.queries = []           # parsed query string of each request
        plex = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urlsplit(self.path)
                seen = parse_qs(parts.query).get("X-Plex-Token", [""])[0]
                plex.requests.append((parts.path, seen, self.headers.get("Range")))
                plex.queries.append(parse_qs(parts.query))
                data = plex.files.get(parts.path)
                if seen != plex.token:
                    return self.send_error(401)
                if data is None:
                    return self.send_error(404)
                status, body, extra = 200, data, {}
                if self.headers.get("Range", "").startswith("bytes="):
                    first, _, last = self.headers["Range"][6:].partition("-")
                    first = int(first)
                    last = int(last) if last else len(data) - 1
                    status, body = 206, data[first:last + 1]
                    extra["Content-Range"] = f"bytes {first}-{last}/{len(data)}"
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def _get(url: str, headers: dict | None = None) -> tuple:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        conn.request("GET", parts.path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


class MediaProxyTests(unittest.TestCase):
    PATH = "/library/parts/1/1234/file.bin"

    def setUp(self):
        self.data = bytes(range(256)) * 4
        self.plex = FakePlex({self.PATH: self.data})
        self.addCleanup(self.plex.stop)
        self.proxy = media_proxy.MediaProxy(lambda: (self.plex.base_url, TOKEN))

    def test_url_exposes_neither_token_nor_plex_address(self):
        url = self.proxy.url_for(self.PATH)
        self.assertNotIn(TOKEN, url)
        self.assertNotIn(str(self.plex._server.server_port), url)
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertTrue(url.endswith(self.PATH))        # extension intact for ffmpeg's probing

    def test_file_is_served_with_the_token_added_upstream(self):
        status, _, body = _get(self.proxy.url_for(self.PATH))
        self.assertEqual((status, body), (200, self.data))
        self.assertEqual(self.plex.requests, [(self.PATH, TOKEN, None)])

    def test_range_requests_are_forwarded_so_seeking_works(self):
        status, headers, body = _get(self.proxy.url_for(self.PATH), {"Range": "bytes=100-199"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.data[100:200])
        self.assertEqual(headers["Content-Range"], "bytes 100-199/1024")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(self.plex.requests[-1][2], "bytes=100-199")

    def test_upstream_errors_pass_through(self):
        status, _, _ = _get(self.proxy.url_for("/library/parts/9/9/missing.bin"))
        self.assertEqual(status, 404)

    def test_only_paths_we_signed_are_forwarded(self):
        good = self.proxy.url_for(self.PATH)
        base, sig = good.rsplit(self.PATH, 1)[0].rsplit("/", 1)
        other = "/library/parts/2/2/other.bin"
        self.plex.files[other] = b"other"
        bad_urls = [
            f"{base}{self.PATH}",                       # no signature at all
            f"{base}/{'0' * 32}{self.PATH}",            # wrong signature
            f"{base}/{sig}{other}",                     # valid signature, different file
            f"{base}/{sig}/library/sections",           # the signature does not unlock the rest of Plex
        ]
        for url in bad_urls:
            with self.subTest(url=url.replace(base, "")):
                self.assertEqual(_get(url)[0], 403)
        self.assertEqual(self.plex.requests, [])        # nothing reached Plex

    def test_hostile_non_ascii_signature_is_rejected_not_a_server_error(self):
        port = urlsplit(self.proxy.url_for(self.PATH)).port
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET /\xe9\xe9\xe9/library/parts/1/1234/file.bin HTTP/1.0\r\n\r\n")
            self.assertIn(b" 403 ", s.recv(200).split(b"\r\n")[0])

    def test_unconfigured_plex_is_a_503(self):
        proxy = media_proxy.MediaProxy(lambda: None)
        self.assertEqual(_get(proxy.url_for(self.PATH))[0], 503)

    def test_unreachable_plex_is_a_502_and_the_token_is_not_logged_or_returned(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()                                    # nothing listens here any more
        proxy = media_proxy.MediaProxy(lambda: (f"http://127.0.0.1:{port}", TOKEN))
        with self.assertLogs(media_proxy.log, level=logging.WARNING) as logs:
            status, headers, body = _get(proxy.url_for(self.PATH))
        self.assertEqual(status, 502)
        self.assertNotIn(TOKEN, body.decode(errors="replace") + str(headers))
        self.assertNotIn(TOKEN, "\n".join(logs.output))


MASTER = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nsession/abc/base/index.m3u8\n"
HLS = "/video/:/transcode/universal"


class PlaylistRewriteTests(unittest.TestCase):
    PLEX = "http://plex.lan:32400"

    def rewrite(self, *lines):
        return media_proxy.rewrite_playlist("\n".join(lines), self.PLEX).splitlines()

    def test_relative_uris_are_kept_and_lose_any_token(self):
        self.assertEqual(
            self.rewrite("#EXTM3U", "session/a/base/index.m3u8", "00000.ts?X-Plex-Token=abc&x=1"),
            ["#EXTM3U", "session/a/base/index.m3u8", "00000.ts?x=1"])

    def test_absolute_plex_urls_are_routed_through_the_app(self):
        self.assertEqual(
            self.rewrite(f"{self.PLEX}{HLS}/session/a/00001.ts?X-Plex-Token=abc",
                         f"{self.PLEX}/library/parts/1/2/file.mp4"),
            ["/plex/hls/session/a/00001.ts", "/plex/part/1/2/file.mp4"])

    def test_uri_attributes_are_rewritten_too(self):
        self.assertEqual(
            self.rewrite(f'#EXT-X-MAP:URI="{self.PLEX}{HLS}/init.mp4?X-Plex-Token=abc",BYTERANGE="9@0"'),
            ['#EXT-X-MAP:URI="/plex/hls/init.mp4",BYTERANGE="9@0"'])

    def test_unknown_plex_paths_lose_the_token_and_other_hosts_are_left_alone(self):
        self.assertEqual(
            self.rewrite(f"{self.PLEX}/other/thing.ts?X-Plex-Token=abc",
                         "https://cdn.example.com/a.ts?x=1"),
            [f"{self.PLEX}/other/thing.ts", "https://cdn.example.com/a.ts?x=1"])


class PlexProxyRouteTests(unittest.TestCase):
    """/plex/<kind>/... and the routes that hand URLs to the browser."""

    PART = "/library/parts/1/1234/file.bin"
    SEGMENT = bytes(range(200))

    def setUp(self):
        self.data = bytes(range(256)) * 4
        self.plex = FakePlex({
            self.PART: self.data,
            "/library/parts/9/99/file.mkv": b"mkv",
            f"{HLS}/start.m3u8": MASTER,
            f"{HLS}/session/abc/base/00000.ts": self.SEGMENT,
        })
        self.addCleanup(self.plex.stop)
        # A real Plex mixes relative URIs, token-carrying ones and absolute URLs to itself.
        self.plex.files[f"{HLS}/session/abc/base/index.m3u8"] = (
            f"#EXTM3U\n#EXTINF:4.0,\n00000.ts?X-Plex-Token={TOKEN}\n"
            f"#EXTINF:4.0,\n{self.plex.base_url}{HLS}/session/abc/base/00001.ts?X-Plex-Token={TOKEN}\n"
            f"#EXT-X-ENDLIST\n").encode()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        cfg = os.path.join(self._tmp.name, "config.json")
        self.write_config(cfg, {"plex_url": self.plex.base_url, "plex_token": TOKEN})
        for p in (patch.object(app, "CONFIG_PATH", cfg),
                  patch.dict(os.environ, {"PLEX_URL": "", "PLEX_TOKEN": ""})):
            p.start()
            self.addCleanup(p.stop)
        self.cfg_path = cfg
        self.client = app.app.test_client()

    @staticmethod
    def write_config(path, cfg):
        with open(path, "w") as f:
            json.dump(cfg, f)

    def test_hls_is_followed_through_the_app_and_plex_only_ever_sees_our_token(self):
        master = self.client.get("/plex/hls/start.m3u8?path=%2Flibrary%2Fmetadata%2F7"
                                 "&session=s1&X-Plex-Token=evil")
        self.assertEqual(master.status_code, 200)
        self.assertEqual(master.mimetype, "application/vnd.apple.mpegurl")
        self.assertIn("session/abc/base/index.m3u8", master.get_data(as_text=True))

        variant = self.client.get("/plex/hls/session/abc/base/index.m3u8")
        text = variant.get_data(as_text=True)
        self.assertEqual([l for l in text.splitlines() if not l.startswith("#")],
                         ["00000.ts", "/plex/hls/session/abc/base/00001.ts"])
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(str(self.plex._server.server_port), text)

        segment = self.client.get("/plex/hls/session/abc/base/00000.ts")
        self.assertEqual((segment.status_code, segment.get_data()), (200, self.SEGMENT))

        # every upstream request carried exactly our token — the client-supplied one was dropped
        self.assertTrue(all(token == TOKEN for _, token, _ in self.plex.requests))
        self.assertEqual(self.plex.queries[0]["X-Plex-Token"], [TOKEN])
        self.assertEqual(self.plex.queries[0]["path"], ["/library/metadata/7"])   # params still pass
        self.assertEqual(self.plex.queries[0]["session"], ["s1"])

    def test_original_files_support_range_requests(self):
        resp = self.client.get("/plex/part/1/1234/file.bin", headers={"Range": "bytes=100-199"})
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.get_data(), self.data[100:200])
        self.assertEqual(resp.headers["Content-Range"], "bytes 100-199/1024")
        self.assertEqual(self.plex.requests, [(self.PART, TOKEN, "bytes=100-199")])

    def test_only_media_paths_are_reachable_and_traversal_is_refused(self):
        for url in ("/plex/library/sections",                  # not an allowed kind
                    "/plex/hls/../../library/sections",
                    "/plex/hls/%2e%2e/%2e%2e/library/sections",
                    "/plex/part/../../video/x",
                    "/plex/nope/anything"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.plex.requests, [])               # nothing reached Plex

    def test_unconfigured_and_unreachable_plex(self):
        self.write_config(self.cfg_path, {})
        self.assertEqual(self.client.get("/plex/part/1/1234/file.bin").status_code, 503)

        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        self.write_config(self.cfg_path, {"plex_url": f"http://127.0.0.1:{port}", "plex_token": TOKEN})
        with self.assertLogs(app.log, level=logging.WARNING) as logs:
            resp = self.client.get("/plex/part/1/1234/file.bin")
        self.assertEqual(resp.status_code, 502)
        self.assertNotIn(TOKEN, resp.get_data(as_text=True) + "\n".join(logs.output))

    def test_routes_that_used_to_expose_the_token_no_longer_do(self):
        item = {"title": "Ep", "grandparentTitle": "Show", "key": "/library/metadata/7",
                "thumb": "/library/metadata/7/thumb/1", "duration": 1000,
                "Media": [{"Part": [{"key": "/library/parts/9/99/file.mkv"}]}]}
        with patch.object(PlexClient, "get_item_metadata", return_value=item), \
                patch.object(PlexClient, "get_server_info", return_value={"machineIdentifier": "m1"}):
            hls = self.client.get("/api/hls_url/7")
            watch = self.client.get("/watch/7")
            stream = self.client.get("/api/stream/7")

        for name, resp in (("hls_url", hls), ("watch", watch), ("stream", stream)):
            with self.subTest(route=name):
                self.assertEqual(resp.status_code, 200)
                self.assertNotIn(TOKEN, resp.get_data(as_text=True))
                self.assertNotIn("Token", resp.get_data(as_text=True))

        hls_url = hls.get_json()["hls_url"]
        self.assertTrue(hls_url.startswith("/plex/hls/start.m3u8?"), hls_url)
        self.assertIn("/plex/hls/start.m3u8?", watch.get_data(as_text=True))
        body = stream.get_json()
        self.assertEqual(body["stream_url"], "http://localhost/plex/part/9/99/file.mkv")
        self.assertIn("/web/index.html#!/server/m1/details", body["plex_web_url"])

        # ...and the URL handed out actually plays, through the app
        self.assertEqual(self.client.get(hls_url).status_code, 200)
        self.assertEqual(self.client.get(body["stream_url"].replace("http://localhost", "")).status_code, 200)

    def test_not_connected_is_reported_by_the_player_routes(self):
        self.write_config(self.cfg_path, {})
        self.assertEqual(self.client.get("/api/hls_url/7").status_code, 503)
        self.assertEqual(self.client.get("/watch/7").status_code, 503)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class ProxiedRelayTests(unittest.TestCase):
    """The real relay, reading a file from a token-protected fake Plex through the gateway."""

    PATH = "/library/parts/5/55/clip.mkv"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        clip = os.path.join(cls._tmp.name, "clip.mkv")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "5",
             "-c:v", "libx264", "-c:a", "aac", clip],
            check=True)
        with open(clip, "rb") as f:
            cls.clip_bytes = f.read()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        for p in (patch.object(db, "DB_PATH", os.path.join(self._tmp.name, "relay.db")),
                  patch.object(streamer, "IDLE_GRACE_S", 0.5),
                  patch.dict(os.environ, {"PLEX_TOKEN": TOKEN})):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        self.addCleanup(streamer.shutdown_all)
        self.plex = FakePlex({self.PATH: self.clip_bytes})
        self.addCleanup(self.plex.stop)
        self.proxy = media_proxy.MediaProxy(lambda: (self.plex.base_url, TOKEN))

    def test_token_never_reaches_ffmpeg_yet_the_stream_plays(self):
        now = int(time.time() * 1000)
        row = dict(
            channel_id="3", rating_key="1", media_part_key=self.PATH, type="episode",
            title="Show", show_title="Show", summary=None, season_number=None,
            episode_number=None, episode_title=None, content_rating=None,
            video_resolution=None, audio_codec=None, video_codec=None, has_subtitles=0,
            thumb_url=None, art_url=None, clear_logo_url=None, duration_ms=5000,
            start_time=now - 1500, end_time=now + 3500,      # join mid-program → ffmpeg must seek
            is_new=0, commercial_media_key=None, commercial_duration=0)
        db.insert_programs([row])

        settings = streamer.StreamSettings.from_config(
            {"resolution": "320x180", "preset": "ultrafast", "video_bitrate": "500k"})
        with patch.object(streamer.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            relay, sub = streamer.subscribe(
                3, settings, lambda prog: self.proxy.url_for(prog["media_part_key"]))
            data, deadline = [], time.time() + 4
            for chunk in sub:
                data.append(chunk)
                if time.time() > deadline:
                    break
            relay.remove_subscriber(sub)

        # What every local user can see of the ffmpeg processes we started:
        self.assertGreaterEqual(popen.call_count, 1)
        for call in popen.call_args_list:
            argv = " ".join(call.args[0])
            self.assertNotIn(TOKEN, argv)
            self.assertNotIn(urlsplit(self.plex.base_url).netloc, argv)    # nor Plex's address
            self.assertNotIn("PLEX_TOKEN", call.kwargs["env"])
        self.assertTrue(any(
            "-i" in call.args[0] and
            call.args[0][call.args[0].index("-i") + 1].startswith("http://127.0.0.1:")
            for call in popen.call_args_list))

        # ...and the media still got through: Plex saw the token, ffmpeg produced real video.
        self.assertTrue(self.plex.requests)
        self.assertTrue(all(token == TOKEN for _, token, _ in self.plex.requests))
        out = os.path.join(self._tmp.name, "out.ts")
        with open(out, "wb") as f:
            f.write(b"".join(data))
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "json", out],
            capture_output=True, text=True, check=True).stdout
        self.assertEqual([s["codec_name"] for s in json.loads(probe)["streams"]], ["h264", "aac"])


if __name__ == "__main__":
    unittest.main()
