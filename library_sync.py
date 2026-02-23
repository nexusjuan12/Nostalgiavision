"""Library sync — fetches Plex content and caches it locally.
Mirrors LibraryCacheManager + LibraryMigrationManager from the Android app."""

import time
import logging
from typing import Callable, Optional

import database as db
from plex_client import PlexClient

log = logging.getLogger(__name__)


def _extract_media_part(plex_item: dict) -> dict:
    """Pull the first media part key, container, codecs, etc."""
    media_list = plex_item.get("Media", [])
    media_part_key = None
    container = None
    audio_codec = None
    video_codec = None
    video_resolution = None
    has_subtitles = 0

    if media_list:
        m = media_list[0]
        container = m.get("container")
        audio_codec = m.get("audioCodec")
        video_codec = m.get("videoCodec")
        video_resolution = m.get("videoResolution")
        parts = m.get("Part", [])
        if parts:
            media_part_key = parts[0].get("key")
            # Check for subtitle streams
            streams = parts[0].get("Stream", [])
            has_subtitles = int(any(
                s.get("streamType") == 3 for s in streams
            ))
    return {
        "media_part_key": media_part_key,
        "container": container,
        "audio_codec": audio_codec,
        "video_codec": video_codec,
        "video_resolution": video_resolution,
        "has_subtitles": has_subtitles,
    }


def _extract_images(client: PlexClient, plex_item: dict) -> dict:
    # Store only the bare Plex path (no token) so the /api/thumb proxy can serve it.
    # e.g. /library/metadata/12345/thumb  — never the full URL with token.
    thumb = plex_item.get("thumb") or plex_item.get("parentThumb")
    art = plex_item.get("art") or plex_item.get("parentArt")
    logo = plex_item.get("Image", [{}])[0].get("url") if plex_item.get("Image") else None
    return {
        "thumb_url": thumb,          # bare path, e.g. /library/metadata/123/thumb
        "art_url": art,
        "clear_logo_url": logo,
    }


def _parse_movie(client: PlexClient, item: dict, library_id: str) -> dict:
    media = _extract_media_part(item)
    images = _extract_images(client, item)
    return {
        "rating_key": str(item.get("ratingKey", "")),
        "key": item.get("key", ""),
        "type": "movie",
        "title": item.get("title", ""),
        "summary": item.get("summary", ""),
        "year": item.get("year"),
        "studio": item.get("studio"),
        "content_rating": item.get("contentRating"),
        "duration": item.get("duration"),
        "show_rating_key": None,
        "show_title": None,
        "season_number": None,
        "episode_number": None,
        "episode_title": None,
        "episode_summary": None,
        "added_at": item.get("addedAt"),
        "library_id": library_id,
        **media,
        **images,
    }


def _parse_episode(client: PlexClient, item: dict, library_id: str) -> dict:
    media = _extract_media_part(item)
    images = _extract_images(client, item)
    return {
        "rating_key": str(item.get("ratingKey", "")),
        "key": item.get("key", ""),
        "type": "episode",
        "title": item.get("grandparentTitle") or item.get("title", ""),
        "summary": item.get("parentSummary") or item.get("summary", ""),
        "year": item.get("year") or item.get("parentYear"),
        "studio": item.get("studio"),
        "content_rating": item.get("contentRating") or item.get("parentContentRating"),
        "duration": item.get("duration"),
        "show_rating_key": str(item.get("grandparentRatingKey", "")) or None,
        "show_title": item.get("grandparentTitle"),
        "season_number": item.get("parentIndex"),
        "episode_number": item.get("index"),
        "episode_title": item.get("title"),
        "episode_summary": item.get("summary"),
        "added_at": item.get("addedAt"),
        "library_id": library_id,
        **media,
        **images,
    }


def _parse_show(client: PlexClient, item: dict, library_id: str) -> dict:
    images = _extract_images(client, item)
    return {
        "rating_key": str(item.get("ratingKey", "")),
        "key": item.get("key", ""),
        "media_part_key": None,
        "type": "show",
        "title": item.get("title", ""),
        "summary": item.get("summary", ""),
        "year": item.get("year"),
        "studio": item.get("studio"),
        "content_rating": item.get("contentRating"),
        "duration": None,
        "show_rating_key": None,
        "show_title": None,
        "season_number": None,
        "episode_number": None,
        "episode_title": None,
        "episode_summary": None,
        "container": None,
        "audio_codec": None,
        "video_codec": None,
        "video_resolution": None,
        "has_subtitles": 0,
        "added_at": item.get("addedAt"),
        "library_id": library_id,
        **images,
    }


def sync_library(
    client: PlexClient,
    library_ids: list,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> dict:
    """Fetch all content from the given Plex library sections and cache locally.

    progress_callback(message, current, total) is called periodically.
    Returns a summary dict.
    """

    def _progress(msg, cur=0, tot=0):
        log.info("%s (%d/%d)", msg, cur, tot)
        if progress_callback:
            progress_callback(msg, cur, tot)

    _progress("Clearing old cache...")
    db.clear_content()

    total_movies = 0
    total_episodes = 0

    for lib_id in library_ids:
        _progress(f"Fetching libraries metadata for section {lib_id}...")
        try:
            libraries = client.get_libraries()
            section = next((l for l in libraries if str(l.get("key")) == str(lib_id)), None)
            lib_type = section.get("type") if section else "unknown"

            if lib_type == "movie":
                _progress(f"Fetching movies from section {lib_id}...")
                raw = client.get_movies(lib_id)
                content_rows = []
                genre_rows = []
                collection_rows = []
                for i, item in enumerate(raw):
                    row = _parse_movie(client, item, lib_id)
                    content_rows.append(row)
                    rk = row["rating_key"]
                    for g in item.get("Genre", []):
                        genre_rows.append({"rating_key": rk, "genre": g.get("tag", "")})
                    for c in item.get("Collection", []):
                        collection_rows.append({
                            "rating_key": rk,
                            "collection_id": str(c.get("ratingKey", c.get("tag", ""))),
                            "collection_name": c.get("tag", ""),
                        })
                    if (i + 1) % 200 == 0:
                        _progress(f"Processed {i+1}/{len(raw)} movies...", i+1, len(raw))

                db.upsert_content_batch(content_rows)
                db.upsert_genres_batch(genre_rows)
                db.upsert_collections_batch(collection_rows)
                total_movies += len(content_rows)
                _progress(f"Cached {len(content_rows)} movies from section {lib_id}", len(raw), len(raw))

            elif lib_type in ("show", "episode"):
                # Fetch episodes (flat list — most efficient)
                _progress(f"Fetching episodes from section {lib_id}...")
                raw_eps = client.get_episodes(lib_id)
                _progress(f"Fetching shows from section {lib_id}...")
                raw_shows = client.get_shows(lib_id)

                content_rows = []
                genre_rows = []
                collection_rows = []

                # Index shows by ratingKey for collection/genre lookup
                show_index = {str(s.get("ratingKey")): s for s in raw_shows}

                for i, item in enumerate(raw_eps):
                    row = _parse_episode(client, item, lib_id)
                    content_rows.append(row)
                    rk = row["rating_key"]
                    # Genres come from parent show
                    show_rk = row.get("show_rating_key")
                    parent = show_index.get(show_rk, {})
                    for g in parent.get("Genre", []):
                        genre_rows.append({"rating_key": rk, "genre": g.get("tag", "")})
                    for c in parent.get("Collection", []):
                        collection_rows.append({
                            "rating_key": rk,
                            "collection_id": str(c.get("ratingKey", c.get("tag", ""))),
                            "collection_name": c.get("tag", ""),
                        })
                    if (i + 1) % 500 == 0:
                        _progress(f"Processed {i+1}/{len(raw_eps)} episodes...", i+1, len(raw_eps))

                # Also store show-level records
                for show in raw_shows:
                    row = _parse_show(client, show, lib_id)
                    content_rows.append(row)
                    rk = row["rating_key"]
                    for g in show.get("Genre", []):
                        genre_rows.append({"rating_key": rk, "genre": g.get("tag", "")})
                    for c in show.get("Collection", []):
                        collection_rows.append({
                            "rating_key": rk,
                            "collection_id": str(c.get("ratingKey", c.get("tag", ""))),
                            "collection_name": c.get("tag", ""),
                        })

                db.upsert_content_batch(content_rows)
                db.upsert_genres_batch(genre_rows)
                db.upsert_collections_batch(collection_rows)
                total_episodes += len(raw_eps)
                _progress(f"Cached {len(raw_eps)} episodes from section {lib_id}", len(raw_eps), len(raw_eps))

            else:
                _progress(f"Skipping section {lib_id} (type={lib_type})")

        except Exception as exc:
            log.error("Failed to sync library section %s: %s", lib_id, exc)
            _progress(f"Error syncing section {lib_id}: {exc}")

    import json
    db.set_cache_metadata(
        cached_at=int(time.time()),
        library_ids=json.dumps(library_ids),
    )

    summary = {"movies": total_movies, "episodes": total_episodes}
    _progress(f"Sync complete — {total_movies} movies, {total_episodes} episodes", 1, 1)
    return summary
