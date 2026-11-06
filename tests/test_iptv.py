"""Coverage for the IPTV feeds (M3U + XMLTV) and the ffmpeg channel relay."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from urllib.parse import urlsplit

import app
import database as db
import iptv
import streamer

BASE = "http://tv.local:5000"
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _program(channel_id="1001", start=0, end=1000, **overrides):
    row = dict(
        channel_id=channel_id, rating_key="1", media_part_key="/library/parts/1/file.mp4",
        type="episode", title="Show", show_title="Show", summary=None,
        season_number=None, episode_number=None, episode_title=None, content_rating=None,
        video_resolution=None, audio_codec=None, video_codec=None, has_subtitles=0,
        thumb_url=None, art_url=None, clear_logo_url=None, duration_ms=end - start,
        start_time=start, end_time=end, is_new=0, commercial_media_key=None,
        commercial_duration=0,
    )
    row.update(overrides)
    return row


class FeedBuilderTests(unittest.TestCase):
    def test_m3u_entries_and_attribute_escaping(self):
        text = iptv.build_m3u([
            {"number": 1001, "name": 'Say "Hi"\nTV', "type": "movie", "logo_url": "/api/logo/1001"},
            {"number": 1002, "name": "Plain", "type": "series", "logo_url": None},
        ], BASE)
        lines = text.splitlines()
        self.assertEqual(lines[0], f'#EXTM3U x-tvg-url="{BASE}/iptv/guide.xml"')
        self.assertEqual(
            lines[1],
            '#EXTINF:-1 tvg-id="nostalgiavision.1001" tvg-name="Say Hi TV" tvg-chno="1001" '
            f'tvg-logo="{BASE}/api/logo/1001" group-title="Movies",Say Hi TV')
        self.assertEqual(lines[2], f"{BASE}/iptv/stream/1001.ts")
        self.assertNotIn("tvg-logo", lines[3])          # no logo → no attribute
        self.assertIn('group-title="Series"', lines[3])
        self.assertEqual(lines[4], f"{BASE}/iptv/stream/1002.ts")

    def test_xmltv_programmes(self):
        channels = [{"number": 1001, "name": "One", "type": "mixed", "logo_url": "/api/logo/1001"}]
        t0 = 1_700_000_000_000
        programs = {"1001": [
            _program(start=t0, end=t0 + 1_800_000, title="Show", episode_title="Pilot",
                     summary="Bell \x08 & <tell>", season_number=2, episode_number=5,
                     content_rating="TV-PG", thumb_url="/library/metadata/9/thumb/1"),
            _program(start=t0 + 1_800_000, end=t0 + 3_600_000, type="movie", title="Film",
                     show_title=None),
        ]}
        root = ET.fromstring(iptv.build_xmltv(channels, programs, BASE))

        channel = root.find("channel")
        self.assertEqual(channel.get("id"), "nostalgiavision.1001")
        self.assertEqual([e.text for e in channel.findall("display-name")], ["One", "1001"])
        self.assertEqual(channel.find("icon").get("src"), f"{BASE}/api/logo/1001")

        first, second = root.findall("programme")
        self.assertEqual(first.get("channel"), "nostalgiavision.1001")
        self.assertEqual(first.get("start"), "20231114221320 +0000")
        self.assertEqual(first.get("stop"), "20231114224320 +0000")
        self.assertEqual(first.find("title").text, "Show")
        self.assertEqual(first.find("sub-title").text, "Pilot")
        self.assertEqual(first.find("desc").text, "Bell  & <tell>")   # control char stripped
        self.assertEqual(first.find("rating/value").text, "TV-PG")
        self.assertEqual(
            {e.get("system"): e.text for e in first.findall("episode-num")},
            {"onscreen": "S02E05", "xmltv_ns": "1.4."})
        self.assertEqual(
            first.find("icon").get("src"),
            f"{BASE}/api/thumb?path=%2Flibrary%2Fmetadata%2F9%2Fthumb%2F1")
        self.assertEqual(second.find("title").text, "Film")
        self.assertEqual(second.find("category").text, "Movie")
        self.assertIsNone(second.find("episode-num"))


class IptvRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = os.path.join(self._tmp.name, "config.json")
        self._write_config({"plex_url": "http://plex.local:32400", "plex_token": "tok"})
        for p in (patch.object(db, "DB_PATH", os.path.join(self._tmp.name, "t.db")),
                  patch.object(app, "CONFIG_PATH", self.config_path)):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        self.client = app.app.test_client()
        self.now = int(time.time() * 1000)

    def _write_config(self, cfg):
        with open(self.config_path, "w") as f:
            json.dump(cfg, f)

    def test_playlist_and_guide_list_only_visible_scheduled_channels(self):
        now = self.now
        db.insert_programs([
            _program("1001", now - 1000, now + 1000),
            _program("1002", now - 1000, now + 1000),
            _program("1003", now - 9000, now - 5000),       # already over → not listed
        ])
        db.set_channel_visibility(1002, False)

        playlist = self.client.get("/iptv/channels.m3u")
        self.assertEqual(playlist.status_code, 200)
        self.assertEqual(playlist.mimetype, "audio/x-mpegurl")
        text = playlist.get_data(as_text=True)
        self.assertIn('tvg-id="nostalgiavision.1001"', text)
        self.assertNotIn("nostalgiavision.1002", text)       # hidden
        self.assertNotIn("nostalgiavision.1003", text)       # nothing left to air
        self.assertIn("http://localhost/iptv/stream/1001.ts", text)

        guide = ET.fromstring(self.client.get("/iptv/guide.xml").data)
        self.assertEqual([c.get("id") for c in guide.findall("channel")], ["nostalgiavision.1001"])
        self.assertEqual(len(guide.findall("programme")), 1)

    def test_public_url_overrides_request_host(self):
        self._write_config({"public_url": "https://tv.example.com/"})
        db.insert_programs([_program("1001", self.now - 1000, self.now + 1000)])
        text = self.client.get("/iptv/channels.m3u").get_data(as_text=True)
        self.assertIn("https://tv.example.com/iptv/stream/1001.ts", text)
        self.assertNotIn("localhost", text)

    def test_stream_rejects_unknown_hidden_and_unconfigured(self):
        self.assertEqual(self.client.get("/iptv/stream/99999.ts").status_code, 404)
        db.set_channel_visibility(1002, False)
        self.assertEqual(self.client.get("/iptv/stream/1002.ts").status_code, 404)

        self._write_config({})
        self.assertEqual(self.client.get("/iptv/stream/1001.ts").status_code, 503)

        self._write_config({"plex_url": "http://plex.local:32400", "plex_token": "tok",
                            "iptv": {"ffmpeg_path": "definitely-not-ffmpeg"}})
        missing = self.client.get("/iptv/stream/1001.ts")
        self.assertEqual(missing.status_code, 503)
        self.assertIn("ffmpeg not found", missing.get_data(as_text=True))

    def test_stream_serves_relay_chunks_and_detaches_viewer(self):
        removed = []

        class FakeRelay:
            def remove_subscriber(self, sub):
                removed.append(sub)

        sub = streamer.Subscriber()
        sub.chunks.put(b"\x47" + b"\x00" * 187)
        sub.closed.set()
        with patch.object(app.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                patch.object(streamer, "subscribe", return_value=(FakeRelay(), sub)) as subscribe:
            with self.client.get("/iptv/stream/1001.ts", headers={"User-Agent": "VLC/3.0.20"},
                                 environ_base={"REMOTE_ADDR": "192.168.1.20"}) as resp:
                body = resp.get_data()                                 # closing fires call_on_close

        self.assertEqual(resp.mimetype, "video/MP2T")
        self.assertEqual(body, b"\x47" + b"\x00" * 187)
        self.assertEqual(removed, [sub])
        number, _settings, source_url = subscribe.call_args.args
        self.assertEqual(number, 1001)
        self.assertEqual(subscribe.call_args.kwargs,
                         {"remote_addr": "192.168.1.20", "user_agent": "VLC/3.0.20"})
        # ffmpeg is handed a signed loopback URL, never the Plex host or token
        local = source_url({"media_part_key": "/library/parts/7/file.mkv"})
        self.assertTrue(local.startswith("http://127.0.0.1:"), local)
        self.assertNotIn("plex.local", local)
        self.assertNotIn("Token", local)
        self.assertEqual(app._media_proxy.verified_path(urlsplit(local).path),
                         "/library/parts/7/file.mkv")
        self.assertIsNone(source_url({"media_part_key": None}))

    def test_activity_lists_connections_with_channel_names(self):
        self.addCleanup(streamer.shutdown_all)
        self.assertEqual(self.client.get("/api/activity").get_json(),
                         {"streams": [], "viewer_count": 0})

        with patch.object(streamer.ChannelRelay, "start"):          # no ffmpeg thread needed
            streamer.subscribe(1001, streamer.StreamSettings(), lambda p: None,
                               remote_addr="192.168.1.20", user_agent="Jellyfin")
            streamer.subscribe(1001, streamer.StreamSettings(), lambda p: None,
                               remote_addr="192.168.1.21", user_agent="VLC")
        data = self.client.get("/api/activity").get_json()

        self.assertEqual(data["viewer_count"], 2)
        (stream,) = data["streams"]
        self.assertEqual((stream["channel"], stream["name"]),
                         (1001, app.PREDEFINED_BY_NUMBER[1001].name))
        self.assertEqual([(v["remote_addr"], v["user_agent"]) for v in stream["viewers"]],
                         [("192.168.1.20", "Jellyfin"), ("192.168.1.21", "VLC")])

        # a channel hidden after someone tuned in is still reported, just unnamed
        db.set_channel_visibility(1001, False)
        (stream,) = self.client.get("/api/activity").get_json()["streams"]
        self.assertEqual(stream["name"], "Channel 1001")

    def test_head_does_not_start_a_relay(self):
        with patch.object(streamer, "subscribe") as subscribe:
            resp = self.client.head("/iptv/stream/1001.ts")
        self.assertEqual(resp.status_code, 200)
        subscribe.assert_not_called()


class ViewerTrackingTests(unittest.TestCase):
    """Who is connected to which relay — no ffmpeg involved."""

    def setUp(self):
        p = patch.object(streamer.ChannelRelay, "start")             # don't spawn the relay thread
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(streamer.shutdown_all)

    def _join(self, number=1001, addr="10.0.0.5", ua="VLC/3.0"):
        return streamer.subscribe(number, streamer.StreamSettings(), lambda p: None,
                                  remote_addr=addr, user_agent=ua)

    def test_active_streams_lists_viewers_per_channel_with_what_is_airing(self):
        relay, _ = self._join(1001, "10.0.0.5", "VLC/3.0")
        self._join(1001, "10.0.0.6", "Lavf/60")
        self._join(1002, "10.0.0.7", "Kodi")
        relay._set_airing({"title": "Show", "episode_title": "Pilot"})

        first, second = streamer.active_streams()

        self.assertEqual([first["channel"], second["channel"]], [1001, 1002])
        self.assertEqual(first["airing"], {"title": "Show", "episode_title": "Pilot"})
        self.assertIsNone(second["airing"])                           # relay hasn't picked anything yet
        self.assertEqual([(v["remote_addr"], v["user_agent"]) for v in first["viewers"]],
                         [("10.0.0.5", "VLC/3.0"), ("10.0.0.6", "Lavf/60")])
        self.assertEqual(len({v["id"] for v in first["viewers"]}), 2)

    def test_bytes_sent_counts_what_the_viewer_actually_consumed(self):
        _, sub = self._join()
        sub.chunks.put(b"x" * 188)
        sub.chunks.put(b"y" * 376)
        sub.closed.set()
        self.assertEqual(len(b"".join(sub)), 564)
        self.assertEqual(sub.snapshot()["bytes_sent"], 564)

    def test_leaving_removes_the_viewer_and_is_logged_once(self):
        relay, sub = self._join()
        with self.assertLogs("streamer", "INFO") as logs:
            relay.remove_subscriber(sub)
            relay.remove_subscriber(sub)                              # e.g. dropped, then response closed
        self.assertEqual(sum("left after" in m for m in logs.output), 1)
        self.assertIsNotNone(sub.left_at)
        self.assertEqual(streamer.active_streams(), [])

    def test_stopped_relay_is_not_reported(self):
        relay, _ = self._join()
        relay.stop()
        self.assertEqual(streamer.active_streams(), [])


class StreamSettingsTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        d = streamer.StreamSettings.from_config(None)
        self.assertEqual((d.width, d.height, d.ffmpeg), (1280, 720, "ffmpeg"))
        s = streamer.StreamSettings.from_config(
            {"resolution": "640x480", "video_bitrate": "1500k", "ffmpeg_path": "/opt/ffmpeg"})
        self.assertEqual((s.width, s.height, s.video_bitrate, s.ffmpeg), (640, 480, "1500k", "/opt/ffmpeg"))
        # a malformed resolution falls back instead of breaking the stream
        self.assertEqual(streamer.StreamSettings.from_config({"resolution": "wide"}).width, 1280)

    def test_http_sources_get_reconnect_flags_but_local_files_do_not(self):
        s = streamer.StreamSettings()
        http = streamer.program_command(s, "http://plex/x.mkv?X-Plex-Token=t", 30, 60, 0)
        self.assertIn("-reconnect", http)
        self.assertEqual(http[http.index("-ss") + 1], "30.000")
        local = streamer.program_command(s, "/tmp/x.mkv", 0, 60, 0)
        self.assertNotIn("-reconnect", local)
        self.assertNotIn("-ss", local)             # no pointless seek at the start of a program

    def test_secrets_are_redacted_from_logged_errors(self):
        self.assertEqual(
            streamer._redact("Error opening http://p/x.mkv?X-Plex-Token=abc123&x=1"),
            "Error opening http://p/x.mkv?X-Plex-Token=<redacted>&x=1")
        self.assertEqual(
            streamer._redact("Error opening http://127.0.0.1:40209/" + "ab12" * 8 + "/library/parts/1/file.mkv"),
            "Error opening http://127.0.0.1:40209/<redacted>/library/parts/1/file.mkv")

    def test_ffmpeg_does_not_inherit_the_plex_token(self):
        with patch.dict(os.environ, {"PLEX_TOKEN": "s3cret", "PATH": "/bin"}):
            env = streamer._child_env()
        self.assertNotIn("PLEX_TOKEN", env)
        self.assertEqual(env["PATH"], "/bin")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class RelayTests(unittest.TestCase):
    """Runs the real ffmpeg pipeline against tiny generated clips."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.clip = os.path.join(cls._tmp.name, "clip.mkv")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "4",
             "-c:v", "libx264", "-c:a", "aac", cls.clip],
            check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        for p in (patch.object(db, "DB_PATH", os.path.join(self._tmp.name, f"{self.id()}.db")),
                  patch.object(streamer, "IDLE_GRACE_S", 0.5)):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        self.addCleanup(streamer.shutdown_all)
        self.settings = streamer.StreamSettings.from_config(
            {"resolution": "320x180", "preset": "ultrafast", "video_bitrate": "500k"})

    def _capture(self, number, seconds, source_url):
        relay, sub = streamer.subscribe(number, self.settings, source_url)
        chunks, deadline = [], time.time() + seconds
        for chunk in sub:
            chunks.append(chunk)
            if time.time() > deadline:
                break
        relay.remove_subscriber(sub)
        return relay, b"".join(chunks)

    def _probe_streams(self, data):
        path = os.path.join(self._tmp.name, f"{self.id()}.ts")
        with open(path, "wb") as f:
            f.write(data)
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
             "-of", "json", path], capture_output=True, text=True, check=True).stdout
        return path, json.loads(out)["streams"]

    def test_rolls_from_one_program_into_the_next_with_rising_timestamps(self):
        now = int(time.time() * 1000)
        db.insert_programs([
            _program("7", now - 1000, now + 2500, title="First", media_part_key=self.clip),
            _program("7", now + 2500, now + 8000, title="Second", media_part_key=self.clip),
        ])
        aired = []

        def source_url(prog):
            aired.append(prog["title"])
            return prog["media_part_key"]

        relay, data = self._capture(7, 7, source_url)

        self.assertEqual(aired[:2], ["First", "Second"])
        self.assertEqual(len(data) % streamer.TS_PACKET, 0)
        self.assertEqual(data[0], 0x47)

        path, streams = self._probe_streams(data)
        self.assertEqual([(s["codec_name"], s.get("width")) for s in streams],
                         [("h264", 320), ("aac", None)])

        dts = [float(line.split(",")[0]) for line in subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "packet=dts_time", "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True).stdout.split() if line.split(",")[0] != "N/A"]
        self.assertGreater(len(dts), 100)
        self.assertEqual(dts, sorted(dts))                  # never steps backwards at the handoff

        # the last viewer leaving shuts the relay (and its ffmpeg) down
        relay._thread.join(timeout=8)
        self.assertFalse(relay._thread.is_alive())
        self.assertTrue(relay.closed)
        self.assertIsNotNone(relay._proc.poll())

    def test_activity_reports_what_is_on_air_and_bytes_delivered(self):
        now = int(time.time() * 1000)
        db.insert_programs([_program("9", now - 1000, now + 30_000, title="Live Show",
                                     episode_title="Ep One", media_part_key=self.clip)])
        relay, sub = streamer.subscribe(9, self.settings, lambda prog: prog["media_part_key"],
                                        remote_addr="10.1.1.1", user_agent="test-agent")
        first = next(iter(sub))
        self.addCleanup(relay.remove_subscriber, sub)

        (stream,) = streamer.active_streams()

        self.assertEqual(stream["channel"], 9)
        self.assertEqual((stream["airing"]["title"], stream["airing"]["episode_title"]),
                         ("Live Show", "Ep One"))
        (viewer,) = stream["viewers"]
        self.assertEqual((viewer["remote_addr"], viewer["user_agent"]), ("10.1.1.1", "test-agent"))
        self.assertGreaterEqual(viewer["bytes_sent"], len(first))

    def test_gap_and_bad_media_air_the_slate_instead_of_dropping(self):
        now = int(time.time() * 1000)
        db.insert_programs([
            _program("8", now - 500, now + 60_000, title="Broken", media_part_key="/nope/missing.mkv"),
        ])
        relay, data = self._capture(8, 4, lambda prog: prog["media_part_key"])

        self.assertGreater(len(data), 50_000)               # kept producing video
        _, streams = self._probe_streams(data)
        self.assertEqual([s["codec_name"] for s in streams], ["h264", "aac"])


if __name__ == "__main__":
    unittest.main()
