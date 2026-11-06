"""IPTV feeds — M3U playlist + XMLTV guide for tuner-style clients (Jellyfin, etc.).

Pure builders: they take plain dicts (channels / program rows) and an absolute
base URL, and return text/bytes. Route wiring lives in app.py.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote

# Characters that are not legal in XML 1.0 (ElementTree does not strip them).
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

_GROUP_BY_TYPE = {"movie": "Movies", "series": "Series", "mixed": "Mixed"}


def channel_id(number: int) -> str:
    """Stable id shared by the M3U `tvg-id` and the XMLTV `<channel id>`."""
    return f"nostalgiavision.{number}"


def _attr(value) -> str:
    """Make a value safe inside a double-quoted M3U attribute (and on a single line)."""
    return re.sub(r"\s+", " ", str(value).replace('"', "")).strip()


def _clean(value) -> str:
    return _XML_ILLEGAL.sub("", str(value))


def _xmltv_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def thumb_proxy_url(base_url: str, thumb_path: str | None) -> str | None:
    """Absolute URL for a Plex thumb, routed through /api/thumb (keeps the token server-side)."""
    if not thumb_path:
        return None
    return f"{base_url}/api/thumb?path={quote(thumb_path, safe='')}"


def build_m3u(channels: list, base_url: str) -> str:
    """Build the playlist.

    channels: dicts with number, name, type and optional logo_url (a path like /api/logo/1001).
    """
    lines = [f'#EXTM3U x-tvg-url="{base_url}/iptv/guide.xml"']
    for ch in channels:
        attrs = [
            f'tvg-id="{channel_id(ch["number"])}"',
            f'tvg-name="{_attr(ch["name"])}"',
            f'tvg-chno="{ch["number"]}"',
        ]
        if ch.get("logo_url"):
            attrs.append(f'tvg-logo="{base_url}{ch["logo_url"]}"')
        attrs.append(f'group-title="{_GROUP_BY_TYPE.get(ch.get("type"), "Mixed")}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{_attr(ch["name"])}')
        lines.append(f'{base_url}/iptv/stream/{ch["number"]}.ts')
    return "\n".join(lines) + "\n"


def build_xmltv(channels: list, programs_by_channel: dict, base_url: str) -> bytes:
    """Build the XMLTV guide.

    programs_by_channel maps str(channel number) → program rows from the programs table.
    """
    tv = ET.Element("tv", {"generator-info-name": "NostalgiaVision"})

    for ch in channels:
        el = ET.SubElement(tv, "channel", {"id": channel_id(ch["number"])})
        ET.SubElement(el, "display-name").text = _clean(ch["name"])
        ET.SubElement(el, "display-name").text = str(ch["number"])
        if ch.get("logo_url"):
            ET.SubElement(el, "icon", {"src": f'{base_url}{ch["logo_url"]}'})

    for ch in channels:
        cid = channel_id(ch["number"])
        for p in programs_by_channel.get(str(ch["number"]), []):
            el = ET.SubElement(tv, "programme", {
                "start": _xmltv_time(p["start_time"]),
                "stop": _xmltv_time(p["end_time"]),
                "channel": cid,
            })
            ET.SubElement(el, "title", {"lang": "en"}).text = _clean(
                p.get("show_title") or p.get("title") or "Untitled")
            if p.get("episode_title"):
                ET.SubElement(el, "sub-title", {"lang": "en"}).text = _clean(p["episode_title"])
            if p.get("summary"):
                ET.SubElement(el, "desc", {"lang": "en"}).text = _clean(p["summary"])
            if p.get("type") == "movie":
                ET.SubElement(el, "category", {"lang": "en"}).text = "Movie"
            thumb = thumb_proxy_url(base_url, p.get("thumb_url"))
            if thumb:
                ET.SubElement(el, "icon", {"src": thumb})

            season, episode = p.get("season_number"), p.get("episode_number")
            if season and episode:
                ET.SubElement(el, "episode-num", {"system": "onscreen"}).text = (
                    f"S{season:02d}E{episode:02d}")
                # xmltv_ns is zero-based: "season.episode.part"
                ET.SubElement(el, "episode-num", {"system": "xmltv_ns"}).text = (
                    f"{season - 1}.{episode - 1}.")

            if p.get("content_rating"):
                rating = ET.SubElement(el, "rating")
                ET.SubElement(rating, "value").text = _clean(p["content_rating"])

    return ET.tostring(tv, encoding="utf-8", xml_declaration=True)
