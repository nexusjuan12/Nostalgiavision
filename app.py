"""Nostalgiavision — Flask web server.

Endpoints:
  GET    /                          → EPG guide page
  GET    /api/status                → connection + cache status
  POST   /api/connect               → save plex URL + token, verify connection
  GET    /api/libraries             → list Plex library sections
  POST   /api/sync                  → sync Plex library to local cache
  GET    /api/channels              → list all channels (predefined + custom)
  GET    /api/guide                 → EPG data for a time window
  POST   /api/schedule/build        → generate/extend schedules
  POST   /api/channels/custom       → create a new custom channel
  PUT    /api/channels/<num>        → edit any channel (predefined override or custom)
  DELETE /api/channels/<num>        → delete/hide a channel
  POST   /api/channels/<num>/restore → restore a hidden predefined channel to defaults
  POST   /api/channels/<num>/logo   → upload a custom logo image
  GET    /api/logo/<num>            → serve a channel logo
  GET    /api/content/count         → content cache statistics
"""

import json
import logging
import requests
import os
import threading
import time
from dataclasses import asdict

from flask import Flask, jsonify, render_template, request, send_from_directory

import database as db
from channels import (PREDEFINED_CHANNELS, PREDEFINED_BY_NUMBER,
                      ChannelConfig, RANDOM)
from library_sync import sync_library
from plex_client import PlexClient
from scheduler import build_schedules, get_matching_content

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

app = Flask(__name__)

# ── App state (in-memory, persisted via config.json + SQLite) ─────────────────

_sync_status = {"running": False, "message": "", "progress": 0, "total": 0}
_schedule_status = {"running": False, "message": "", "progress": 0, "total": 0}

# Full-response cache for /api/status — prevents thread pile-up under heavy polling.
# A single blocking lock ensures only ONE thread ever builds the response; all others
# wait, then immediately return the already-cached result.  TTL must be ≥ poll interval.
_status_response_cache = {"data": None, "at": 0.0}
_STATUS_TTL = 5.0          # seconds between actual status computations
_status_lock = threading.Lock()

# Plex reachability sub-cache (only one thread hits the network at a time)
_conn_cache = {"ok": False, "checked_at": 0.0}
_CONN_CACHE_TTL = 30.0
_conn_check_lock = threading.Lock()


def _load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def _save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def _get_client() -> PlexClient | None:
    cfg = _load_config()
    url = cfg.get("plex_url", "").strip().rstrip("/")
    token = cfg.get("plex_token", "").strip()
    if not url or not token:
        return None
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    return PlexClient(url, token)


LOGOS_DIR = os.path.join(os.path.dirname(__file__), "data", "logos")


def _logo_url(number: int) -> str | None:
    """Return /api/logo/<num> if a logo file exists for this channel, else None."""
    for ext in ("png", "jpg", "jpeg", "webp", "gif"):
        if os.path.exists(os.path.join(LOGOS_DIR, f"{number}.{ext}")):
            return f"/api/logo/{number}"
    return None


def _all_channel_configs() -> list:
    """Return predefined + custom channel configs, excluding hidden ones.

    Predefined channels may be overridden by entries in channel_overrides.
    """
    hidden = db.get_hidden_channels()
    overrides = db.get_all_channel_overrides()
    result = []

    for c in PREDEFINED_CHANNELS:
        if c.number in hidden:
            continue
        if c.number in overrides:
            try:
                result.append(ChannelConfig(**overrides[c.number]))
                continue
            except Exception as exc:
                log.warning("Bad override for channel %d: %s", c.number, exc)
        result.append(c)

    for row in db.get_custom_channels():
        try:
            data = json.loads(row["config_json"])
            result.append(ChannelConfig(**data))
        except Exception as exc:
            log.warning("Skipping malformed custom channel %d: %s", row["number"], exc)

    return sorted(result, key=lambda c: c.number)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/image/<path:filename>")
def serve_image_asset(filename: str):
    """Serve UI image assets from the local ./image directory."""
    image_dir = os.path.join(os.path.dirname(__file__), "image")
    return send_from_directory(image_dir, filename)


@app.route("/api/status")
def api_status():
    # Fast path: return cached response without any lock contention.
    now = time.time()
    if _status_response_cache["data"] is not None and (now - _status_response_cache["at"]) < _STATUS_TTL:
        return _status_response_cache["data"]

    # Slow path: acquire the lock and compute.  Because only ONE thread runs this
    # block at a time, all other concurrent requests queue up and then hit the
    # fast path above once the first thread finishes.
    with _status_lock:
        # Re-check inside the lock — another thread may have just computed it.
        now = time.time()
        if _status_response_cache["data"] is not None and (now - _status_response_cache["at"]) < _STATUS_TTL:
            return _status_response_cache["data"]

        cfg = _load_config()
        client = _get_client()
        # Check Plex reachability at most every _CONN_CACHE_TTL seconds.
        if client and (now - _conn_cache["checked_at"]) > _CONN_CACHE_TTL:
            if _conn_check_lock.acquire(blocking=False):
                try:
                    _conn_cache["checked_at"] = time.time()
                    _conn_cache["ok"] = client.verify_connection()
                except Exception:
                    _conn_cache["ok"] = False
                finally:
                    _conn_check_lock.release()
        connected = _conn_cache["ok"] if client else False
        cache = db.get_cache_metadata()
        counts = db.get_content_count()
        resp = jsonify({
            "connected": connected,
            "plex_url": cfg.get("plex_url", ""),
            "has_token": bool(cfg.get("plex_token")),
            "cache": cache,
            "content_counts": counts,
            "sync_status": _sync_status,
            "schedule_status": _schedule_status,
        })
        _status_response_cache["data"] = resp
        _status_response_cache["at"] = time.time()
        return resp


@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.get_json(force=True)
    url = (data.get("plex_url") or "").strip().rstrip("/")
    token = (data.get("plex_token") or "").strip()
    if not url or not token:
        return jsonify({"ok": False, "error": "plex_url and plex_token are required"}), 400

    # Ensure protocol prefix
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url

    client = PlexClient(url, token)
    try:
        ok = client.verify_connection()
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 401:
            return jsonify({"ok": False, "error": "Invalid Plex token — check your X-Plex-Token"}), 401
        return jsonify({"ok": False, "error": f"Plex server error ({status})"}), 502
    except requests.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": "Cannot reach Plex server — check the URL and port"}), 502
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    if not ok:
        return jsonify({"ok": False, "error": "Could not connect — check URL and token"}), 401

    cfg = _load_config()
    cfg["plex_url"] = url
    cfg["plex_token"] = token
    _save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/libraries")
def api_libraries():
    client = _get_client()
    if not client:
        return jsonify({"error": "Not connected — enter your Plex URL and token in Settings first"}), 401
    try:
        libs = client.get_libraries()
        return jsonify({"libraries": libs})
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 401:
            return jsonify({"error": "Invalid Plex token — update it in Settings"}), 401
        return jsonify({"error": f"Plex server returned {status}"}), 502
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Cannot reach Plex server — check the URL in Settings"}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/sync", methods=["POST"])
def api_sync():
    global _sync_status
    if _sync_status["running"]:
        return jsonify({"ok": False, "error": "Sync already in progress"}), 409

    data = request.get_json(force=True)
    library_ids = data.get("library_ids", [])
    if not library_ids:
        cfg = _load_config()
        library_ids = cfg.get("library_ids", [])
    if not library_ids:
        return jsonify({"ok": False, "error": "No library_ids provided"}), 400

    # Persist selected library IDs
    cfg = _load_config()
    cfg["library_ids"] = library_ids
    _save_config(cfg)

    client = _get_client()
    if not client:
        return jsonify({"ok": False, "error": "Not connected"}), 401

    def _run():
        global _sync_status
        _sync_status = {"running": True, "message": "Starting...", "progress": 0, "total": 0}
        try:
            def progress(msg, cur, tot):
                _sync_status["message"] = msg
                _sync_status["progress"] = cur
                _sync_status["total"] = tot

            sync_library(client, library_ids, progress)
        except Exception as exc:
            _sync_status["message"] = f"Error: {exc}"
            log.exception("Sync failed")
        finally:
            _sync_status["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync started"})


@app.route("/api/channels")
def api_channels():
    hidden = db.get_hidden_channels()
    overrides = db.get_all_channel_overrides()
    result = []

    for c in PREDEFINED_CHANNELS:
        if c.number in overrides:
            try:
                d = overrides[c.number]
            except Exception:
                d = asdict(c)
        else:
            d = asdict(c)
        d["is_hidden"] = c.number in hidden
        d["is_custom"] = False
        d["is_overridden"] = c.number in overrides
        d["logo_url"] = _logo_url(c.number)
        result.append(d)

    for row in db.get_custom_channels():
        try:
            d = json.loads(row["config_json"])
            d["is_hidden"] = d["number"] in hidden
            d["is_custom"] = True
            d["is_overridden"] = False
            d["logo_url"] = _logo_url(d["number"])
            result.append(d)
        except Exception:
            pass

    return jsonify({"channels": result})


@app.route("/api/guide")
def api_guide():
    """Return EPG data.

    Query params:
      start  — unix timestamp in seconds (default: now)
      hours  — window size in hours (default: 4)
    """
    try:
        start_sec = int(request.args.get("start", int(time.time())))
        hours = int(request.args.get("hours", 4))
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400

    start_ms = start_sec * 1000
    end_ms = start_ms + hours * 3_600_000

    rows = db.get_all_programs(start_ms, end_ms)

    # Group by channel_id
    by_channel: dict = {}
    for r in rows:
        cid = r["channel_id"]
        by_channel.setdefault(cid, []).append(r)

    # Build channel list with current program info
    hidden = db.get_hidden_channels()
    channels_out = []
    for cfg in PREDEFINED_CHANNELS:
        if cfg.number in hidden:
            continue
        cid = str(cfg.number)
        progs = by_channel.get(cid, [])
        channels_out.append({
            "number": cfg.number,
            "name": cfg.name,
            "type": cfg.type,
            "logo_url": _logo_url(cfg.number),
            "programs": progs,
        })
    # Custom channels
    for row in db.get_custom_channels():
        try:
            d = json.loads(row["config_json"])
            cid = str(d["number"])
            progs = by_channel.get(cid, [])
            channels_out.append({
                "number": d["number"],
                "name": d["name"],
                "type": d.get("type", "mixed"),
                "logo_url": _logo_url(d["number"]),
                "programs": progs,
            })
        except Exception:
            pass

    return jsonify({
        "start": start_ms,
        "end": end_ms,
        "channels": channels_out,
    })


@app.route("/api/schedule/build", methods=["POST"])
def api_schedule_build():
    global _schedule_status
    if _schedule_status["running"]:
        return jsonify({"ok": False, "error": "Schedule generation already running"}), 409

    data = request.get_json(force=True) or {}
    days = int(data.get("days", _load_config().get("schedule_days_ahead", 3)))
    align = False  # back-to-back scheduling only; alignment removed
    channel_numbers = data.get("channels")  # None = all channels

    configs = _all_channel_configs()
    if channel_numbers:
        configs = [c for c in configs if c.number in channel_numbers]

    def _run():
        global _schedule_status
        _schedule_status = {"running": True, "message": "Starting...", "progress": 0, "total": len(configs)}
        try:
            def progress(name, idx, total):
                _schedule_status["message"] = f"Scheduling {name}..."
                _schedule_status["progress"] = idx
                _schedule_status["total"] = total

            summary = build_schedules(configs, days=days, align=align, progress_callback=progress)
            total_programs = sum(summary.values())
            _schedule_status["message"] = f"Done — {total_programs} programs scheduled"
        except Exception as exc:
            _schedule_status["message"] = f"Error: {exc}"
            log.exception("Schedule build failed")
        finally:
            _schedule_status["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Schedule generation started"})


@app.route("/api/schedule/clear", methods=["POST"])
def api_schedule_clear():
    db.clear_all_programs()
    return jsonify({"ok": True})


@app.route("/api/channels/custom", methods=["POST"])
def api_create_custom_channel():
    data = request.get_json(force=True)
    required = ("number", "name")
    for f in required:
        if f not in data:
            return jsonify({"error": f"Missing field: {f}"}), 400

    number = int(data["number"])
    if number in PREDEFINED_BY_NUMBER:
        return jsonify({"error": "Channel number conflicts with a predefined channel"}), 409

    # Build a ChannelConfig to validate the data
    try:
        cfg = ChannelConfig(
            number=number,
            name=data["name"],
            genres=data.get("genres", []),
            studios=data.get("studios", []),
            content_ratings=data.get("content_ratings", []),
            keywords=data.get("keywords", []),
            collection_ids=data.get("collection_ids", []),
            collection_names=data.get("collection_names", []),
            type=data.get("type", "mixed"),
            max_year=data.get("max_year"),
            max_movies_per_day=data.get("max_movies_per_day"),
            sorting_method=data.get("sorting_method", RANDOM),
            marathons_enabled=bool(data.get("marathons_enabled", False)),
            is_custom=True,
            custom_media_rating_keys=data.get("custom_media_rating_keys", []),
            excluded_rating_keys=data.get("excluded_rating_keys", []),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    db.save_custom_channel(number, json.dumps(asdict(cfg)))

    # Preview how many items match
    matching = get_matching_content(cfg)
    return jsonify({"ok": True, "matching_count": len(matching)})


@app.route("/api/channels/<int:number>", methods=["PUT"])
def api_edit_channel(number: int):
    """Edit any channel.

    For predefined channels an override record is stored so the defaults are
    preserved and can be restored later.  For custom channels the record is
    updated in-place.
    """
    data = request.get_json(force=True)
    data["number"] = number          # ensure number is not changed by client
    data.setdefault("is_custom", number not in PREDEFINED_BY_NUMBER)

    try:
        cfg = ChannelConfig(**{
            k: v for k, v in data.items()
            if k in ChannelConfig.__dataclass_fields__
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    cfg_dict = asdict(cfg)
    if number in PREDEFINED_BY_NUMBER:
        db.save_channel_override(number, json.dumps(cfg_dict))
    else:
        db.save_custom_channel(number, json.dumps(cfg_dict))

    matching = get_matching_content(cfg)
    return jsonify({"ok": True, "matching_count": len(matching)})


@app.route("/api/channels/<int:number>", methods=["DELETE"])
def api_delete_channel(number: int):
    """Delete or hide a channel.

    Custom channels are permanently removed.
    Predefined channels are hidden (they can be restored via /restore).
    In both cases the channel's scheduled programs are wiped.
    """
    if number in PREDEFINED_BY_NUMBER:
        db.set_channel_visibility(number, False)
    else:
        db.delete_custom_channel(number)
    db.delete_channel_programs_from(str(number), 0)
    return jsonify({"ok": True})


@app.route("/api/channels/<int:number>/restore", methods=["POST"])
def api_restore_channel(number: int):
    """Restore a hidden predefined channel to its default config."""
    if number not in PREDEFINED_BY_NUMBER:
        return jsonify({"error": "Not a predefined channel"}), 400
    db.set_channel_visibility(number, True)
    db.delete_channel_override(number)
    return jsonify({"ok": True})


def _detect_image_type(data: bytes) -> str | None:
    """Return image type string from magic bytes, or None if unrecognised."""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    if data[:3] == b'\xff\xd8\xff':
        return "jpeg"
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return "gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "webp"
    return None


@app.route("/api/channels/<int:number>/logo", methods=["POST"])
def api_upload_logo(number: int):
    """Upload a custom logo image for a channel (multipart/form-data, field: logo)."""
    os.makedirs(LOGOS_DIR, exist_ok=True)

    if "logo" not in request.files:
        return jsonify({"error": "No file in 'logo' field"}), 400

    f = request.files["logo"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    data = f.read()
    img_type = _detect_image_type(data)
    if img_type not in ("png", "jpeg", "gif", "webp"):
        return jsonify({"error": "File must be PNG, JPEG, GIF, or WebP"}), 400

    ext = "jpg" if img_type == "jpeg" else img_type

    # Remove any old logo for this channel
    for old_ext in ("png", "jpg", "jpeg", "webp", "gif"):
        old = os.path.join(LOGOS_DIR, f"{number}.{old_ext}")
        if os.path.exists(old):
            os.remove(old)

    path = os.path.join(LOGOS_DIR, f"{number}.{ext}")
    with open(path, "wb") as fp:
        fp.write(data)

    return jsonify({"ok": True, "logo_url": f"/api/logo/{number}"})


@app.route("/api/channels/<int:number>/logo", methods=["DELETE"])
def api_delete_logo(number: int):
    """Remove the custom logo for a channel."""
    removed = False
    for ext in ("png", "jpg", "jpeg", "webp", "gif"):
        p = os.path.join(LOGOS_DIR, f"{number}.{ext}")
        if os.path.exists(p):
            os.remove(p)
            removed = True
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/logo/<int:number>")
def api_serve_logo(number: int):
    """Serve a channel's custom logo image."""
    from flask import send_file
    for ext in ("png", "jpg", "jpeg", "webp", "gif"):
        p = os.path.join(LOGOS_DIR, f"{number}.{ext}")
        if os.path.exists(p):
            return send_file(p)
    return "", 404


@app.route("/api/channels/<int:number>/hide", methods=["POST"])
def api_hide_channel(number: int):
    db.set_channel_visibility(number, False)
    return jsonify({"ok": True})


@app.route("/api/channels/<int:number>/show", methods=["POST"])
def api_show_channel(number: int):
    db.set_channel_visibility(number, True)
    return jsonify({"ok": True})


@app.route("/api/content/count")
def api_content_count():
    return jsonify(db.get_content_count())


@app.route("/api/channels/<int:number>/preview")
def api_channel_preview(number: int):
    """Return matching content count for a channel."""
    overrides = db.get_all_channel_overrides()
    if number in overrides:
        try:
            cfg = ChannelConfig(**overrides[number])
        except Exception:
            cfg = PREDEFINED_BY_NUMBER.get(number)
    else:
        cfg = PREDEFINED_BY_NUMBER.get(number)
    if not cfg:
        for row in db.get_custom_channels():
            d = json.loads(row["config_json"])
            if d["number"] == number:
                cfg = ChannelConfig(**d)
                break
    if not cfg:
        return jsonify({"error": "Channel not found"}), 404
    matching = get_matching_content(cfg)
    return jsonify({"number": number, "matching_count": len(matching)})


# ── Thumbnail proxy ───────────────────────────────────────────────────────────

from flask import Response as FlaskResponse
from urllib.parse import urlparse

@app.route("/api/thumb")
def api_thumb():
    """Proxy a Plex thumbnail through Flask to avoid CORS and token exposure.

    Query param: path — accepts either a bare Plex path (/library/metadata/123/thumb)
    or a full URL (http://server/.../thumb?token=...) — handles both formats so the
    proxy works regardless of which format is stored in the database.
    """
    raw = request.args.get("path", "").strip()
    if not raw:
        return "", 400

    client = _get_client()
    if not client:
        return "", 401

    # Normalise: extract just the path component from whatever we were given,
    # then rebuild a clean authenticated URL using the configured server.
    if raw.startswith("http://") or raw.startswith("https://"):
        bare_path = urlparse(raw).path   # strip host + query params
    else:
        bare_path = raw

    url = f"{client.base_url}{bare_path}?X-Plex-Token={client.token}"

    try:
        resp = client._session.get(url, timeout=(5, 10))
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        return FlaskResponse(
            resp.content,
            status=200,
            content_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except Exception as exc:
        log.warning("Thumb proxy failed for %s: %s", bare_path, exc)
        return "", 502


# ── Video player (HLS with server-side audio transcode) ───────────────────────

@app.route("/watch/<rating_key>")
def watch(rating_key: str):
    """Serve a full-screen HLS video player for a Plex media item.

    Uses Plex's universal transcoder with directStream=1 so video is copied
    as-is but audio is transcoded to AAC — fixes AC3/DTS playback in browsers.
    """
    import uuid
    from urllib.parse import urlencode

    cfg = _load_config()
    plex_url = cfg.get("plex_url", "")
    token = cfg.get("plex_token", "")

    if not plex_url or not token:
        return "Not connected to Plex", 503

    session_id = uuid.uuid4().hex[:16]
    client_id = f"nostalgiavision-{session_id}"

    hls_params = urlencode({
        "X-Plex-Token": token,
        "X-Plex-Product": "Nostalgiavision",
        "X-Plex-Version": "0.3.17",
        "X-Plex-Platform": "Chrome",
        "X-Plex-Client-Identifier": client_id,
        "path": f"/library/metadata/{rating_key}",
        "protocol": "hls",
        "directPlay": "0",
        "directStream": "1",   # copy video stream, transcode audio → AAC
        "mediaIndex": "0",
        "partIndex": "0",
        "fastSeek": "1",
        "subtitleSize": "100",
        "session": session_id,
    })
    hls_url = f"{plex_url}/video/:/transcode/universal/start.m3u8?{hls_params}"

    # Fetch metadata for the title display
    client = _get_client()
    title = "Nostalgiavision"
    thumb_path = None
    try:
        item = client.get_item_metadata(rating_key) if client else None
        if item:
            show = item.get("grandparentTitle") or item.get("title", "")
            ep = item.get("title", "") if item.get("grandparentTitle") else ""
            sn = item.get("parentIndex")
            en = item.get("index")
            ep_label = f"S{sn:02d}E{en:02d} — {ep}" if sn and en and ep else ep
            title = f"{show}  {ep_label}".strip() if ep_label else show
            thumb_path = item.get("thumb")
    except Exception:
        pass

    thumb_url = f"/api/thumb?path={requests.utils.quote(thumb_path, safe='')}" if thumb_path else ""

    return render_template("player.html",
                           hls_url=hls_url,
                           title=title,
                           thumb_url=thumb_url,
                           rating_key=rating_key)


@app.route("/api/hls_url/<rating_key>")
def api_hls_url(rating_key: str):
    """Return HLS transcode URL + metadata for embedding in the guide player."""
    import uuid
    from urllib.parse import urlencode

    cfg = _load_config()
    plex_url = cfg.get("plex_url", "")
    token = cfg.get("plex_token", "")

    if not plex_url or not token:
        return jsonify({"error": "Not connected to Plex"}), 503

    session_id = uuid.uuid4().hex[:16]
    client_id = f"nostalgiavision-{session_id}"

    hls_params = urlencode({
        "X-Plex-Token": token,
        "X-Plex-Product": "Nostalgiavision",
        "X-Plex-Version": "0.3.17",
        "X-Plex-Platform": "Chrome",
        "X-Plex-Client-Identifier": client_id,
        "path": f"/library/metadata/{rating_key}",
        "protocol": "hls",
        "directPlay": "0",
        "directStream": "1",
        "mediaIndex": "0",
        "partIndex": "0",
        "fastSeek": "1",
        "subtitleSize": "100",
        "session": session_id,
    })
    hls_url = f"{plex_url}/video/:/transcode/universal/start.m3u8?{hls_params}"

    client = _get_client()
    title = ""
    thumb_url = None
    duration_ms = None
    try:
        item = client.get_item_metadata(rating_key) if client else None
        if item:
            show = item.get("grandparentTitle") or item.get("title", "")
            ep = item.get("title", "") if item.get("grandparentTitle") else ""
            sn = item.get("parentIndex")
            en = item.get("index")
            ep_label = f"S{sn:02d}E{en:02d} — {ep}" if sn and en and ep else ep
            title = f"{show}  {ep_label}".strip() if ep_label else show
            thumb_path = item.get("thumb")
            if thumb_path:
                thumb_url = f"/api/thumb?path={requests.utils.quote(thumb_path, safe='')}"
            duration_ms = item.get("duration")
    except Exception:
        pass

    return jsonify({
        "hls_url": hls_url,
        "title": title,
        "thumb_url": thumb_url,
        "duration_ms": duration_ms,
    })


# ── Playback ──────────────────────────────────────────────────────────────────

@app.route("/api/clients")
def api_clients():
    """List available Plex clients/players on the server."""
    client = _get_client()
    if not client:
        return jsonify({"error": "Not connected"}), 401
    try:
        clients = client.get_clients()
        return jsonify({"clients": clients})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/server_info")
def api_server_info():
    """Return server identity (machineIdentifier, friendlyName, etc.)."""
    client = _get_client()
    if not client:
        return jsonify({"error": "Not connected"}), 401
    try:
        info = client.get_server_info()
        cfg = _load_config()
        return jsonify({
            "machine_id": info.get("machineIdentifier", ""),
            "friendly_name": info.get("friendlyName", ""),
            "plex_url": cfg.get("plex_url", ""),
            "version": info.get("version", ""),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/stream/<rating_key>")
def api_stream(rating_key: str):
    """Return stream URL and Plex Web URL for a media item.

    The direct stream URL can be opened in any media player (VLC, MPV, etc.)
    or in the browser if the format is compatible.
    The Plex Web URL opens the item in the Plex Web UI.
    """
    client = _get_client()
    if not client:
        return jsonify({"error": "Not connected"}), 401

    try:
        item = client.get_item_metadata(rating_key)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    if not item:
        return jsonify({"error": "Item not found"}), 404

    cfg = _load_config()
    plex_url = cfg.get("plex_url", "")
    token = cfg.get("plex_token", "")

    # Build direct stream URL from the first media part
    stream_url = None
    media_list = item.get("Media", [])
    if media_list:
        parts = media_list[0].get("Part", [])
        if parts:
            part_key = parts[0].get("key", "")
            stream_url = f"{plex_url}{part_key}?X-Plex-Token={token}"

    # Build Plex Web URL (opens item in the local Plex web UI)
    try:
        server_info = client.get_server_info()
        machine_id = server_info.get("machineIdentifier", "")
        item_key = item.get("key", f"/library/metadata/{rating_key}")
        encoded_key = requests.utils.quote(item_key, safe="")
        plex_web_url = (
            f"{plex_url}/web/index.html#!/server/{machine_id}"
            f"/details?key={encoded_key}&X-Plex-Token={token}"
        )
    except Exception:
        plex_web_url = None

    return jsonify({
        "rating_key": rating_key,
        "title": item.get("title", ""),
        "type": item.get("type", ""),
        "stream_url": stream_url,
        "plex_web_url": plex_web_url,
        "duration": item.get("duration"),
    })


@app.route("/api/play_on_client", methods=["POST"])
def api_play_on_client():
    """Send a play command to a specific Plex client.

    Body: { "client_identifier": "...", "rating_key": "...",
            "media_type": "movie|episode", "offset_ms": 0 }
    """
    client = _get_client()
    if not client:
        return jsonify({"error": "Not connected"}), 401

    data = request.get_json(force=True)
    client_identifier = data.get("client_identifier", "")
    rating_key = data.get("rating_key", "")
    media_type = data.get("media_type", "movie")
    offset_ms = int(data.get("offset_ms", 0))

    if not client_identifier or not rating_key:
        return jsonify({"error": "client_identifier and rating_key are required"}), 400

    try:
        ok = client.play_on_client(client_identifier, rating_key, media_type, offset_ms)
        return jsonify({"ok": ok})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    cfg = _load_config()
    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", 5000))
    log.info("Nostalgiavision starting on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)
