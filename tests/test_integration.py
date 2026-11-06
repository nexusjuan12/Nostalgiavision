"""Integration regressions: proxy prefixes, validation, and relay lifecycle."""
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
import database as db
import media_proxy
import streamer
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.test import Client
from werkzeug.wrappers import Response
from test_iptv import _program, HAVE_FFMPEG
from test_media_proxy import FakePlex, TOKEN, HLS


class ProxyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(db, 'DB_PATH', os.path.join(self.tmp.name, 'test.db')),
                  patch.object(app, 'LOGOS_DIR', os.path.join(self.tmp.name, 'logos')),
                  patch.object(app, '_load_config', return_value={})):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()

    def test_mounted_feeds_artwork_and_browser_playback(self):
        now = int(time.time() * 1000)
        db.insert_programs([_program('1001', now - 1000, now + 60000,
                                    thumb_url='/library/metadata/1/thumb')])
        Path(app.LOGOS_DIR).mkdir()
        Path(app.LOGOS_DIR, '1001.png').write_bytes(b'logo')
        self.plex = FakePlex({f'{HLS}/start.m3u8': (
            f'#EXTM3U\n{HLS}/session/a.ts?X-Plex-Token={TOKEN}\n'
            f'{HLS}/session/b.ts\n').encode(),
            f'{HLS}/session/a.ts': b'segment',
            '/library/metadata/1/thumb': b'thumb'})
        self.addCleanup(self.plex.stop)
        client = Client(DispatcherMiddleware(Response(status=404), {'/ntv': app.app}), Response)
        config = {'plex_url': self.plex.base_url, 'plex_token': TOKEN}
        item = {'title': 'Example', 'thumb': '/library/metadata/1/thumb'}
        with patch.object(app, '_load_config', return_value=config), \
                patch.object(app.PlexClient, 'get_item_metadata', return_value=item):
            m3u = client.get('/ntv/iptv/channels.m3u').text
            xml = client.get('/ntv/iptv/guide.xml').text
            self.assertIn('http://localhost/ntv/iptv/stream/1001.ts', m3u)
            self.assertIn('http://localhost/ntv/api/logo/1001', m3u)
            self.assertIn('http://localhost/ntv/api/thumb?', xml)
            with client.get('/ntv/api/logo/1001') as logo:
                self.assertEqual(logo.data, b'logo')
            self.assertEqual(client.get('/ntv/api/thumb?path=/library/metadata/1/thumb').data, b'thumb')
            playlist = client.get('/ntv/plex/hls/start.m3u8').text
            self.assertIn('/ntv/plex/hls/session/a.ts', playlist)
            self.assertNotIn(TOKEN, playlist)
            self.assertEqual(client.get('/ntv/plex/hls/session/a.ts').data, b'segment')
            self.assertIn("new URL('../', document.baseURI)", client.get('/ntv/watch/1').text)
            self.assertEqual(client.get('/ntv/api/hls_url/1').status_code, 200)

    def test_stripping_proxy_uses_public_url_prefix_for_hls(self):
        self.plex = FakePlex({f'{HLS}/start.m3u8': f'#EXTM3U\n{HLS}/session/a.ts\n'.encode()})
        self.addCleanup(self.plex.stop)
        config = {'plex_url': self.plex.base_url, 'plex_token': TOKEN,
                  'public_url': 'https://example.com/ntv/'}
        with patch.object(app, '_load_config', return_value=config):
            client = app.app.test_client()
            self.assertIn('/ntv/plex/hls/session/a.ts', client.get('/plex/hls/start.m3u8').text)
            self.assertIn('https://example.com/ntv/iptv/guide.xml', client.get('/iptv/channels.m3u').text)

    def test_nested_traversal_and_non_media_thumb_paths_are_rejected(self):
        client = app.app.test_client()
        with patch.object(app, '_get_client', return_value=app.PlexClient('http://plex.invalid', TOKEN)), \
                patch.object(media_proxy, 'open_plex') as upstream:
            for path in ('%252e%252e/library/sections', 'a%255c..%255csecret', 'a//b'):
                self.assertEqual(client.get('/plex/part/' + path).status_code, 404)
            for path in ('/library/sections', '/library/metadata/../sections', '//evil/metadata/1'):
                self.assertEqual(client.get('/api/thumb', query_string={'path': path}).status_code, 400)
            upstream.assert_not_called()
        proxy = media_proxy.MediaProxy(lambda: None)
        for path in ('/library/sections', '/library/parts/../sections', '/library/parts/%252e%252e/x'):
            with self.assertRaises(ValueError):
                proxy.url_for(path)

    def test_redirects_are_not_followed_or_exposed_to_clients(self):
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/other?X-Plex-Token=' + TOKEN)
                self.end_headers()
                self.wfile.write(TOKEN.encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        with patch.object(app, '_load_config', return_value={'plex_url': base, 'plex_token': TOKEN}):
            resp = app.app.test_client().get('/plex/part/1/file.mp4')
        self.assertEqual(resp.status_code, 502)
        self.assertNotIn('Location', resp.headers)
        self.assertNotIn(TOKEN, resp.text)
        self.assertEqual(len(hits), 1)


class SlowViewerTests(unittest.TestCase):
    def test_evicted_last_viewer_starts_idle_cleanup_timer(self):
        relay = streamer.ChannelRelay(1, streamer.StreamSettings(), lambda p: None)
        sub = relay.add_subscriber()
        sub.chunks = queue.Queue(maxsize=1)
        relay._broadcast(b'a')
        relay._broadcast(b'b')
        self.assertTrue(sub.closed.is_set())
        self.assertIsNotNone(relay._idle_since)
        with patch.object(streamer, 'IDLE_GRACE_S', -1):
            self.assertTrue(relay._should_stop())


@unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class LateJoinTests(unittest.TestCase):
    def test_real_http_disconnect_releases_viewer_and_ffmpeg(self):
        import requests
        from werkzeug.serving import make_server

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(db, 'DB_PATH', os.path.join(directory, 'http.db')), \
                patch.object(streamer, 'IDLE_GRACE_S', 0.3):
            db.init_db()
            clip = os.path.join(directory, 'http.mkv')
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=160x120:rate=25', '-f', 'lavfi', '-i',
                            'sine=frequency=440', '-t', '8', '-c:v', 'libx264',
                            '-c:a', 'aac', clip], check=True, timeout=20)
            path = '/library/parts/1/http.mkv'
            plex = FakePlex({path: Path(clip).read_bytes()})
            now = int(time.time() * 1000)
            db.insert_programs([_program('1001', now - 500, now + 20000, media_part_key=path)])
            config = {'plex_url': plex.base_url, 'plex_token': TOKEN,
                      'iptv': {'resolution': '160x120', 'preset': 'ultrafast'}}
            server = make_server('127.0.0.1', 0, app.app, threaded=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with patch.object(app, '_load_config', return_value=config):
                    with requests.get(f'http://127.0.0.1:{server.server_port}/iptv/stream/1001.ts',
                                      stream=True, timeout=(3, 10)) as response:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(next(response.iter_content(streamer.CHUNK_BYTES))[0], 0x47)
                        relay = streamer._relays[1001]
                    deadline = time.monotonic() + 8
                    while not relay.closed and time.monotonic() < deadline:
                        time.sleep(0.1)
                    relay._thread.join(timeout=3)
                    self.assertTrue(relay.closed)
                    self.assertFalse(relay._thread.is_alive())
                    self.assertIsNotNone(relay._proc.poll())
                    self.assertEqual(streamer.active_streams(), [])
            finally:
                streamer.shutdown_all()
                server.shutdown()
                server.server_close()
                plex.stop()

    def test_late_viewer_decodes_and_disconnect_reaps_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(db, 'DB_PATH', os.path.join(directory, 'test.db')), \
                patch.object(streamer, 'IDLE_GRACE_S', 0.3):
            db.init_db()
            clip = os.path.join(directory, 'clip.mkv')
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=160x120:rate=25', '-f', 'lavfi', '-i',
                            'sine=frequency=440', '-t', '12', '-c:v', 'libx264',
                            '-c:a', 'aac', clip], check=True, timeout=20)
            now = int(time.time() * 1000)
            db.insert_programs([_program('12', now - 1000, now + 15000, media_part_key=clip)])
            settings = streamer.StreamSettings(width=160, height=120, preset='ultrafast')
            relay, first = streamer.subscribe(12, settings, lambda p: p['media_part_key'])
            try:
                first.chunks.get(timeout=10)
                second_relay, second = streamer.subscribe(12, settings, lambda p: p['media_part_key'])
                self.assertIs(second_relay, relay)
                relay.remove_subscriber(first)
                captured = bytearray()
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    captured.extend(second.chunks.get(timeout=10))
                output = os.path.join(directory, 'late.ts')
                Path(output).write_bytes(captured)
                probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                        'stream=codec_name', '-of', 'json', output],
                                       capture_output=True, text=True, check=True, timeout=15)
                self.assertEqual([s['codec_name'] for s in json.loads(probe.stdout)['streams']],
                                 ['h264', 'aac'])
                # Actually decode the late-joined stream, rather than only recognizing its headers.
                decoded = subprocess.run(['ffmpeg', '-v', 'fatal', '-i', output, '-map', '0:v:0',
                                          '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                                         capture_output=True, check=True, timeout=15)
                self.assertEqual(len(decoded.stdout), 160 * 120 * 3)
                relay.remove_subscriber(second)
                relay._thread.join(timeout=8)
                self.assertFalse(relay._thread.is_alive())
                self.assertIsNotNone(relay._proc.poll())
                self.assertNotIn(12, streamer._relays)
            finally:
                streamer.shutdown_all()
                relay._thread.join(timeout=8)
