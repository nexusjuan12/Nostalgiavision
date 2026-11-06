"""Live channel streams — turns the EPG schedule into continuous MPEG-TS.

A tuner client (Jellyfin, VLC, ...) expects one never-ending stream per channel,
but the schedule is a list of separate files. Each channel being watched gets one
shared "relay": a thread that looks up what is airing right now, runs ffmpeg on that
file from the correct offset, and fans the MPEG-TS output out to every connected
viewer. When a program ends the relay moves on to the next one; during gaps (or if a
file fails) it airs the test-pattern slate instead of dropping the connection.

Every program is re-encoded to the same codec/resolution/frame rate so the stream
never changes format mid-flight, whatever the source files are.
"""

import atexit
import collections
import itertools
import logging
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass

import database as db

log = logging.getLogger(__name__)

TS_PACKET = 188
CHUNK_BYTES = TS_PACKET * 56            # keep chunks packet-aligned so late joiners can sync
SUBSCRIBER_BACKLOG = 600                # chunks a slow viewer may lag before being dropped
IDLE_GRACE_S = 10.0                     # keep running after the last viewer leaves (probe → reconnect)
STALL_S = 20.0                          # kill an ffmpeg that produces no output for this long
MIN_HEALTHY_RUN_S = 2.0                 # a program run shorter than this counts as a failure
SCHEDULE_LOOKAHEAD_MS = 6 * 3_600_000

_TOKEN_RE = re.compile(r"(X-Plex-Token=)[^&\s'\"]+")
# The media gateway's per-process signature (see media_proxy.py) — no need to leave it in logs.
_SIGNED_URL_RE = re.compile(r"(http://127\.0\.0\.1:\d+/)[0-9a-f]{32}/")
# ffmpeg parses untrusted media files and has no use for credentials, so it doesn't inherit them.
_SECRET_ENV = ("PLEX_TOKEN",)


@dataclass(frozen=True)
class StreamSettings:
    ffmpeg: str = "ffmpeg"
    width: int = 1280
    height: int = 720
    video_bitrate: str = "3000k"
    audio_bitrate: str = "128k"
    preset: str = "veryfast"
    slate_image: str | None = None

    @classmethod
    def from_config(cls, section: dict | None, slate_image: str | None = None) -> "StreamSettings":
        """Build from the optional "iptv" object in config.json; unknown/bad keys fall back to defaults."""
        section = section or {}
        defaults = cls()
        width, height = defaults.width, defaults.height
        m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", str(section.get("resolution", "")))
        if m:
            width, height = int(m[1]), int(m[2])
        return cls(
            ffmpeg=section.get("ffmpeg_path") or defaults.ffmpeg,
            width=width,
            height=height,
            video_bitrate=str(section.get("video_bitrate") or defaults.video_bitrate),
            audio_bitrate=str(section.get("audio_bitrate") or defaults.audio_bitrate),
            preset=section.get("preset") or defaults.preset,
            slate_image=slate_image,
        )


# ── ffmpeg command lines ───────────────────────────────────────────────────────

def _redact(text: str) -> str:
    return _SIGNED_URL_RE.sub(r"\1<redacted>/", _TOKEN_RE.sub(r"\1<redacted>", text))


def _child_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}


def _video_filter(s: StreamSettings) -> str:
    return (f"scale={s.width}:{s.height}:force_original_aspect_ratio=decrease,"
            f"pad={s.width}:{s.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"fps=30000/1001,format=yuv420p")


def _output_args(s: StreamSettings, ts_offset: float) -> list:
    return [
        "-vf", _video_filter(s),
        "-c:v", "libx264", "-preset", s.preset, "-b:v", s.video_bitrate,
        # A keyframe every ~2 s so a viewer joining mid-stream waits at most that long.
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-af", "aresample=48000:async=1:first_pts=0",
        "-c:a", "aac", "-b:a", s.audio_bitrate, "-ar", "48000", "-ac", "2",
        # Each ffmpeg run starts its clock at zero; shift it so the stream's timestamps keep rising.
        "-output_ts_offset", f"{ts_offset:.3f}",
        "-f", "mpegts", "-mpegts_flags", "+resend_headers+initial_discontinuity",
        "pipe:1",
    ]


_COMMON = ["-hide_banner", "-loglevel", "error", "-nostdin"]


def program_command(s: StreamSettings, url: str, offset_s: float, duration_s: float,
                    ts_offset: float) -> list:
    cmd = [s.ffmpeg, *_COMMON, "-re"]
    if url.startswith(("http://", "https://")):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if offset_s > 0.5:
        cmd += ["-ss", f"{offset_s:.3f}"]
    cmd += ["-i", url, "-t", f"{duration_s:.3f}", "-map", "0:v:0", "-map", "0:a:0?"]
    return cmd + _output_args(s, ts_offset)


def slate_command(s: StreamSettings, duration_s: float, ts_offset: float) -> list:
    if s.slate_image:
        video_in = ["-re", "-loop", "1", "-framerate", "30000/1001", "-i", s.slate_image]
    else:
        video_in = ["-re", "-f", "lavfi", "-i",
                    f"smptebars=size={s.width}x{s.height}:rate=30000/1001"]
    cmd = [s.ffmpeg, *_COMMON, *video_in,
           "-re", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
           "-t", f"{duration_s:.3f}", "-map", "0:v:0", "-map", "1:a:0"]
    return cmd + _output_args(s, ts_offset)


# ── Viewers ────────────────────────────────────────────────────────────────────

_viewer_ids = itertools.count(1)


class Subscriber:
    """One connected viewer: a bounded queue of MPEG-TS chunks, plus who/what it is."""

    def __init__(self, remote_addr: str = "", user_agent: str = ""):
        self.chunks: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_BACKLOG)
        self.closed = threading.Event()     # set by the relay: stop serving this viewer
        self.id = next(_viewer_ids)
        self.remote_addr = remote_addr
        self.user_agent = user_agent
        self.connected_at = time.time()
        self.left_at: float | None = None   # set once, when the viewer detaches
        self.bytes_sent = 0                 # bytes handed to the HTTP response so far

    def __iter__(self):
        """Yield chunks until the relay drops this viewer or shuts down."""
        while True:
            try:
                chunk = self.chunks.get(timeout=1.0)
            except queue.Empty:
                if self.closed.is_set():
                    return
                continue
            self.bytes_sent += len(chunk)
            yield chunk

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "remote_addr": self.remote_addr,
            "user_agent": self.user_agent,
            "connected_at": self.connected_at,
            "duration_s": round(time.time() - self.connected_at, 1),
            "bytes_sent": self.bytes_sent,
        }


# ── Relay ──────────────────────────────────────────────────────────────────────

class ChannelRelay:
    def __init__(self, number: int, settings: StreamSettings, source_url):
        """source_url(program_row) → URL/path ffmpeg can read, or None if unavailable."""
        self.number = number
        self.settings = settings
        self._source_url = source_url
        self._lock = threading.Lock()
        self._subs: set = set()
        self._idle_since: float | None = time.monotonic()
        self._closed = False
        self._proc: subprocess.Popen | None = None
        self._ts_offset = 0.0
        self._airing: dict | None = None     # what the relay is putting on air right now
        self._thread = threading.Thread(target=self._run, name=f"relay-{number}", daemon=True)

    # -- viewers --

    def add_subscriber(self, remote_addr: str = "", user_agent: str = "") -> Subscriber | None:
        with self._lock:
            if self._closed:
                return None
            sub = Subscriber(remote_addr, user_agent)
            self._subs.add(sub)
            self._idle_since = None
            viewers = len(self._subs)
        log.info("Channel %d: viewer #%d connected from %s (%s) — %d watching",
                 self.number, sub.id, remote_addr or "unknown", user_agent or "no user-agent", viewers)
        return sub

    def remove_subscriber(self, sub: Subscriber):
        with self._lock:
            self._subs.discard(sub)
            if not self._subs and self._idle_since is None:
                self._idle_since = time.monotonic()
            first_time = sub.left_at is None
            if first_time:
                sub.left_at = time.time()
            viewers = len(self._subs)
        if first_time:
            log.info("Channel %d: viewer #%d (%s) left after %.0fs, %.1f MB — %d watching",
                     self.number, sub.id, sub.remote_addr or "unknown",
                     sub.left_at - sub.connected_at, sub.bytes_sent / 1e6, viewers)

    def status(self) -> dict:
        """Who is watching this channel and what is on air, for the activity view."""
        with self._lock:
            viewers = [s.snapshot() for s in self._subs]
            airing = dict(self._airing) if self._airing else None
        viewers.sort(key=lambda v: (v["connected_at"], v["id"]))
        return {"channel": self.number, "airing": airing, "viewers": viewers}

    def _broadcast(self, chunk: bytes):
        with self._lock:
            for sub in list(self._subs):
                try:
                    sub.chunks.put_nowait(chunk)
                except queue.Full:
                    log.warning("Channel %d: dropping a viewer that fell too far behind", self.number)
                    self._subs.discard(sub)
                    sub.closed.set()

    # -- lifecycle --

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self):
        self._thread.start()

    def stop(self):
        with self._lock:
            self._closed = True
            subs, self._subs = list(self._subs), set()
        for sub in subs:
            sub.closed.set()
        self._kill_proc()

    def _should_stop(self) -> bool:
        with self._lock:
            if self._closed:
                return True
            if not self._subs and self._idle_since is not None \
                    and time.monotonic() - self._idle_since > IDLE_GRACE_S:
                self._closed = True
                return True
            return False

    def _set_airing(self, airing: dict | None):
        with self._lock:
            self._airing = airing

    def _kill_proc(self):
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    # -- ffmpeg --

    def _run_ffmpeg(self, cmd: list, label: str) -> float:
        """Run one ffmpeg process, broadcasting its output. Returns wall-clock seconds it ran."""
        started = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, env=_child_env(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            log.error("Channel %d: cannot start ffmpeg (%s)", self.number, exc)
            return 0.0
        self._proc = proc
        last_output = [started]
        err_tail: collections.deque = collections.deque(maxlen=8)

        def pump():
            while True:
                data = proc.stdout.read(CHUNK_BYTES)
                if not data:
                    return
                last_output[0] = time.monotonic()
                self._broadcast(data)

        def drain_stderr():
            for line in proc.stderr:
                err_tail.append(line.decode("utf-8", "replace").rstrip())

        threads = [threading.Thread(target=pump, daemon=True),
                   threading.Thread(target=drain_stderr, daemon=True)]
        for t in threads:
            t.start()

        while proc.poll() is None:
            if self._should_stop():
                break
            if time.monotonic() - last_output[0] > STALL_S:
                log.warning("Channel %d: ffmpeg stalled during %s; restarting", self.number, label)
                break
            time.sleep(0.2)
        self._kill_proc()
        for t in threads:
            t.join(timeout=2)
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass

        ran = time.monotonic() - started
        # Keep timestamps rising across runs; the margin covers -re letting ffmpeg run slightly ahead.
        self._ts_offset += ran + 1.0
        if proc.returncode not in (0, None) and not self._closed and err_tail:
            log.warning("Channel %d: ffmpeg exited %s during %s: %s", self.number,
                        proc.returncode, label, _redact(" | ".join(err_tail)))
        return ran

    def _air_slate(self, seconds: float, why: str):
        seconds = max(1.0, seconds)
        log.info("Channel %d: airing slate for %.0fs (%s)", self.number, seconds, why)
        self._set_airing({"slate": why})
        self._run_ffmpeg(slate_command(self.settings, seconds, self._ts_offset), "slate")

    def _run(self):
        log.info("Channel %d: relay started", self.number)
        try:
            while not self._should_stop():
                now_ms = int(time.time() * 1000)
                rows = db.get_programs_for_channel(
                    str(self.number), now_ms, now_ms + SCHEDULE_LOOKAHEAD_MS)
                if not rows:
                    self._air_slate(5, "nothing scheduled")
                    continue
                prog = rows[0]
                if prog["start_time"] > now_ms:
                    gap_s = (prog["start_time"] - now_ms) / 1000
                    self._air_slate(min(gap_s, 10), "gap before next program")
                    continue

                remaining_s = (prog["end_time"] - now_ms) / 1000
                if remaining_s < 1.0:
                    time.sleep(remaining_s + 0.05)      # let the next program come around
                    continue

                url = self._source_url(prog)
                if not url:
                    self._air_slate(min(remaining_s, 5), "no media for program")
                    continue

                offset_s = (now_ms - prog["start_time"]) / 1000
                label = f'"{prog.get("title")}" @ {offset_s:.0f}s'
                log.info("Channel %d: airing %s", self.number, label)
                self._set_airing({
                    "title": prog.get("title"),
                    "episode_title": prog.get("episode_title"),
                    "start_time": prog["start_time"],
                    "end_time": prog["end_time"],
                })
                ran = self._run_ffmpeg(
                    program_command(self.settings, url, offset_s, remaining_s, self._ts_offset),
                    label)
                if ran < MIN_HEALTHY_RUN_S and not self._closed:
                    # Bad file / offset past EOF: don't spin, hold the slate briefly.
                    left_s = (prog["end_time"] - time.time() * 1000) / 1000
                    if left_s > 1.0:
                        self._air_slate(min(left_s, 5), "program failed to play")
        except Exception:
            log.exception("Channel %d: relay crashed", self.number)
        finally:
            self.stop()
            log.info("Channel %d: relay stopped", self.number)


# ── Registry ───────────────────────────────────────────────────────────────────

_relays: dict = {}
_registry_lock = threading.Lock()


def subscribe(number: int, settings: StreamSettings, source_url,
              remote_addr: str = "", user_agent: str = "") -> tuple:
    """Attach a viewer to a channel's relay, starting the relay if needed → (relay, subscriber)."""
    with _registry_lock:
        relay = _relays.get(number)
        sub = relay.add_subscriber(remote_addr, user_agent) if relay else None
        if sub is None:
            relay = ChannelRelay(number, settings, source_url)
            _relays[number] = relay
            sub = relay.add_subscriber(remote_addr, user_agent)
            relay.start()
        return relay, sub


def active_streams() -> list:
    """Status of every channel that currently has at least one viewer, lowest channel first."""
    with _registry_lock:
        relays = list(_relays.values())
    statuses = (r.status() for r in relays if not r.closed)
    return sorted((s for s in statuses if s["viewers"]), key=lambda s: s["channel"])


def shutdown_all():
    with _registry_lock:
        relays = list(_relays.values())
        _relays.clear()
    for relay in relays:
        relay.stop()


atexit.register(shutdown_all)
