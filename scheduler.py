"""Schedule generation engine — mirrors ChannelRepository scheduling logic.

Implements the 4 sorting algorithms (RANDOM, CYCLIC_SHUFFLE, BLOCK_SHUFFLE,
BLOCK_CYCLIC) and converts matched content into ProgramEntity dicts ready for
database insertion.
"""

import random
import time
import logging
from typing import Optional

import database as db
from channels import ChannelConfig, RANDOM, CYCLIC_SHUFFLE, BLOCK_SHUFFLE, BLOCK_CYCLIC

log = logging.getLogger(__name__)

QUARTER_HOUR_MS = 15 * 60 * 1000   # 900 000 ms
DEFAULT_DURATION_MS = 30 * 60 * 1000  # 30 min fallback
DAY_MS = 24 * 60 * 60 * 1000


# ── Content matching ───────────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    return s.lower().strip() if s else ""


def _content_matches_channel(item: dict, cfg: ChannelConfig,
                              genres_map: dict, collections_map: dict) -> bool:
    """Return True if a content item satisfies the channel's filter rules."""
    rk = item["rating_key"]

    # Type filter
    if cfg.type == "movie" and item["type"] != "movie":
        return False
    if cfg.type == "series" and item["type"] not in ("episode", "show"):
        return False
    if cfg.type == "music" and item["type"] != "track":
        return False
    if cfg.type == "weather":
        return False  # weather channel gets special treatment

    # Year filter
    if cfg.max_year and item.get("year") and item["year"] > cfg.max_year:
        return False

    # Content rating filter
    if cfg.content_ratings:
        cr = item.get("content_rating") or ""
        if not any(_normalise(r) in _normalise(cr) or _normalise(cr) in _normalise(r)
                   for r in cfg.content_ratings):
            return False

    # Genre filter (item must have at least one matching genre)
    if cfg.genres:
        item_genres = [_normalise(g) for g in genres_map.get(rk, [])]
        if not any(_normalise(g) in item_genres for g in cfg.genres):
            return False

    # Studio filter
    if cfg.studios:
        item_studio = _normalise(item.get("studio") or "")
        if not any(_normalise(s) in item_studio or item_studio in _normalise(s)
                   for s in cfg.studios):
            return False

    # Keyword filter (check title + show_title)
    if cfg.keywords:
        haystack = _normalise(
            (item.get("title") or "") + " " +
            (item.get("show_title") or "") + " " +
            (item.get("episode_title") or "")
        )
        if not any(_normalise(kw) in haystack for kw in cfg.keywords):
            return False

    # Collection filter
    if cfg.collection_ids or cfg.collection_names:
        item_cols = collections_map.get(rk, [])
        matched = False
        for col in item_cols:
            if cfg.collection_ids and col["collection_id"] in cfg.collection_ids:
                matched = True
                break
            if cfg.collection_names and any(
                _normalise(n) in _normalise(col["collection_name"])
                for n in cfg.collection_names
            ):
                matched = True
                break
        if not matched:
            return False

    # Custom whitelist (if set, only those keys are allowed)
    if cfg.custom_media_rating_keys:
        if rk not in cfg.custom_media_rating_keys:
            return False

    # Custom exclusion
    if rk in cfg.excluded_rating_keys:
        return False

    return True


def get_matching_content(cfg: ChannelConfig) -> list:
    """Query the local content cache and return items that match this channel."""
    if cfg.type == "movie":
        raw = db.get_all_content("movie")
    elif cfg.type == "series":
        raw = db.get_all_content("episode")
    elif cfg.type in ("mixed", None, ""):
        raw = db.get_all_content("movie") + db.get_all_content("episode")
    elif cfg.type == "music":
        raw = db.get_all_content("track")
    else:
        return []

    if not raw:
        return []

    keys = [r["rating_key"] for r in raw]
    genres_map = db.get_genres_for_keys(keys)
    collections_map = db.get_collections_for_keys(keys)

    return [
        item for item in raw
        if _content_matches_channel(item, cfg, genres_map, collections_map)
    ]


# ── Time alignment ─────────────────────────────────────────────────────────────

def _align_duration(duration_ms: int, align: bool) -> int:
    """Round up duration to the next 15-minute boundary."""
    if not align or duration_ms <= 0:
        return duration_ms or DEFAULT_DURATION_MS
    remainder = duration_ms % QUARTER_HOUR_MS
    if remainder == 0:
        return duration_ms
    return duration_ms + (QUARTER_HOUR_MS - remainder)


# ── Program entity builder ─────────────────────────────────────────────────────

def _build_program(item: dict, channel_id: str, start_time: int,
                   align: bool) -> tuple:
    """Build a program dict and return (program_dict, next_start_time)."""
    raw_dur = item.get("duration") or DEFAULT_DURATION_MS
    dur = _align_duration(raw_dur, align)
    end_time = start_time + dur

    prog = {
        "channel_id": channel_id,
        "rating_key": item.get("rating_key"),
        "media_part_key": item.get("media_part_key"),
        "type": item.get("type"),
        "title": item.get("show_title") or item.get("title", ""),
        "show_title": item.get("show_title"),
        "summary": item.get("episode_summary") or item.get("summary", ""),
        "season_number": item.get("season_number"),
        "episode_number": item.get("episode_number"),
        "episode_title": item.get("episode_title"),
        "content_rating": item.get("content_rating"),
        "video_resolution": item.get("video_resolution"),
        "audio_codec": item.get("audio_codec"),
        "video_codec": item.get("video_codec"),
        "has_subtitles": item.get("has_subtitles", 0),
        "thumb_url": item.get("thumb_url"),
        "art_url": item.get("art_url"),
        "clear_logo_url": item.get("clear_logo_url"),
        "duration_ms": raw_dur,       # actual Plex duration (pre-alignment)
        "start_time": start_time,
        "end_time": end_time,
        "is_new": 0,
        "commercial_media_key": None,
        "commercial_duration": 0,
    }
    return prog, end_time


# ── 4 Scheduling algorithms ────────────────────────────────────────────────────

def _generate_random(content: list, channel_id: str, start_time: int,
                     end_time: int, align: bool) -> list:
    """Pure random selection from the content pool."""
    if not content:
        return []
    pool = list(content)
    random.shuffle(pool)
    programs = []
    current = start_time
    idx = 0
    while current < end_time:
        item = pool[idx % len(pool)]
        prog, current = _build_program(item, channel_id, current, align)
        programs.append(prog)
        idx += 1
    return programs


def _generate_cyclic_shuffle(content: list, channel_id: str, start_time: int,
                              end_time: int, align: bool) -> list:
    """Episodes grouped by season; seasons shuffled, then cycled."""
    if not content:
        return []

    # Separate episodes (group by season) from non-episodic content
    by_season: dict = {}
    other = []
    for item in content:
        sn = item.get("season_number")
        if sn is not None:
            by_season.setdefault(sn, []).append(item)
        else:
            other.append(item)

    if not by_season:
        # No season info — fall back to random
        return _generate_random(content, channel_id, start_time, end_time, align)

    # Shuffle within each season
    for sn in by_season:
        random.shuffle(by_season[sn])

    seasons = list(by_season.keys())
    random.shuffle(seasons)

    programs = []
    current = start_time
    season_idx = 0
    ep_idx: dict = {sn: 0 for sn in seasons}

    while current < end_time:
        sn = seasons[season_idx % len(seasons)]
        ep_list = by_season[sn]
        item = ep_list[ep_idx[sn] % len(ep_list)]
        prog, current = _build_program(item, channel_id, current, align)
        programs.append(prog)
        ep_idx[sn] += 1
        if ep_idx[sn] >= len(ep_list):
            season_idx += 1

    return programs


def _split_into_blocks(content: list, block_count: int) -> list:
    """Split content list into roughly equal blocks."""
    if not content or block_count <= 0:
        return [content]
    size = max(1, len(content) // block_count)
    blocks = []
    for i in range(block_count):
        start = i * size
        end = start + size if i < block_count - 1 else len(content)
        blocks.append(list(content[start:end]))
    return [b for b in blocks if b]


def _generate_block_shuffle(content: list, channel_id: str, start_time: int,
                             end_time: int, align: bool) -> list:
    """Content divided into blocks; blocks shuffled; cycle through blocks."""
    if not content:
        return []
    pool = list(content)
    random.shuffle(pool)
    block_count = max(2, min(5, len(pool) // 10 + 1))
    blocks = _split_into_blocks(pool, block_count)
    for b in blocks:
        random.shuffle(b)
    block_order = list(range(len(blocks)))
    random.shuffle(block_order)

    programs = []
    current = start_time
    block_pos = 0
    item_idx = [0] * len(blocks)

    while current < end_time:
        bi = block_order[block_pos % len(block_order)]
        block = blocks[bi]
        item = block[item_idx[bi] % len(block)]
        prog, current = _build_program(item, channel_id, current, align)
        programs.append(prog)
        item_idx[bi] += 1
        block_pos += 1

    return programs


def _generate_block_cyclic(content: list, channel_id: str, start_time: int,
                            end_time: int, align: bool) -> list:
    """Like block_shuffle but block order is fixed (sequential cycling)."""
    if not content:
        return []
    pool = list(content)
    block_count = max(2, min(5, len(pool) // 10 + 1))
    blocks = _split_into_blocks(pool, block_count)
    for b in blocks:
        random.shuffle(b)

    programs = []
    current = start_time
    block_pos = 0
    item_idx = [0] * len(blocks)

    while current < end_time:
        bi = block_pos % len(blocks)
        block = blocks[bi]
        item = block[item_idx[bi] % len(block)]
        prog, current = _build_program(item, channel_id, current, align)
        programs.append(prog)
        item_idx[bi] += 1
        block_pos += 1

    return programs


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_channel_schedule(
    cfg: ChannelConfig,
    start_time: int,
    end_time: int,
    align: bool = True,
) -> list:
    """Generate a schedule for one channel.

    Args:
        cfg: Channel configuration.
        start_time: Schedule window start (ms since epoch).
        end_time: Schedule window end (ms since epoch).
        align: Whether to snap durations to quarter-hour boundaries.

    Returns:
        List of program dicts ready for db.insert_programs().
    """
    channel_id = str(cfg.number)
    content = get_matching_content(cfg)

    if not content:
        log.warning("Channel %d (%s): no matching content found", cfg.number, cfg.name)
        return []

    method = cfg.sorting_method or RANDOM
    log.info("Generating schedule for channel %d (%s) with %s, %d items, window=%dh",
             cfg.number, cfg.name, method, len(content),
             (end_time - start_time) // 3_600_000)

    if method == CYCLIC_SHUFFLE:
        return _generate_cyclic_shuffle(content, channel_id, start_time, end_time, align)
    elif method == BLOCK_SHUFFLE:
        return _generate_block_shuffle(content, channel_id, start_time, end_time, align)
    elif method == BLOCK_CYCLIC:
        return _generate_block_cyclic(content, channel_id, start_time, end_time, align)
    else:  # RANDOM (default)
        return _generate_random(content, channel_id, start_time, end_time, align)


def build_schedules(
    configs: list,
    days: int = 3,
    align: bool = False,
    progress_callback=None,
) -> dict:
    """Generate and persist schedules for all given channel configs.

    Args:
        configs: List of ChannelConfig objects.
        days: How many days ahead to schedule.
        align: Quarter-hour alignment toggle.
        progress_callback: Optional callable(channel_name, idx, total).

    Returns:
        Summary dict with program counts per channel.
    """
    # Start at the beginning of the current hour
    now_ms = int(time.time() * 1000)
    # Round down to nearest hour
    hour_ms = 3_600_000
    start_ms = (now_ms // hour_ms) * hour_ms
    end_ms = start_ms + days * DAY_MS

    # Delete old/expired programs first
    db.delete_expired_programs(now_ms)

    summary = {}
    total = len(configs)

    for idx, cfg in enumerate(configs):
        if progress_callback:
            progress_callback(cfg.name, idx, total)

        channel_id = str(cfg.number)

        # Wipe all not-yet-started programs so re-builds after a library sync
        # always produce a fresh schedule with correct rating_keys from the
        # current content table.  Any currently-airing program is preserved so
        # there is no playback interruption.
        db.delete_channel_programs_from(channel_id, now_ms)

        # Schedule from after the currently-airing program's end (if one exists)
        # so there is no gap or overlap at the handoff point.
        latest = db.get_latest_program_end(channel_id)
        gen_start = latest if latest and latest > start_ms else start_ms

        if gen_start >= end_ms:
            summary[cfg.number] = 0
            continue

        programs = generate_channel_schedule(cfg, gen_start, end_ms, align)
        if programs:
            db.insert_programs(programs)
        summary[cfg.number] = len(programs)
        log.info("Channel %d: inserted %d programs", cfg.number, len(programs))

    if progress_callback:
        progress_callback("Done", total, total)

    return summary
