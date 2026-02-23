"""Plex API client — mirrors PlexApiService + ApiClient from the Android app."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Optional
import logging

log = logging.getLogger(__name__)

PLEX_HEADERS = {
    "X-Plex-Product": "Nostalgiavision",
    "X-Plex-Version": "0.3.17",
    "X-Plex-Platform": "Python",
    "X-Plex-Device-Name": "Nostalgiavision-Desktop",
    "Accept": "application/json",
}


def _make_session(timeout: tuple = (7, 15)) -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.timeout = timeout
    return session


class PlexClient:
    """Thin wrapper around the Plex Media Server HTTP API."""

    def __init__(self, base_url: str, token: str):
        # Normalise base URL
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._session = _make_session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        headers = {**PLEX_HEADERS, "X-Plex-Token": self.token}
        params = params or {}
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, headers=headers, params=params,
                                     timeout=(7, 15))
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.JSONDecodeError:
            log.warning("Non-JSON response from %s", url)
            return {}
        except requests.exceptions.RequestException as exc:
            log.error("Plex request failed [%s]: %s", url, exc)
            raise

    def _paginate(self, path: str, params: Optional[dict] = None,
                  page_size: int = 500) -> list:
        """Fetch all pages of a Plex library endpoint."""
        results = []
        params = dict(params or {})
        params["X-Plex-Container-Size"] = page_size
        start = 0
        while True:
            params["X-Plex-Container-Start"] = start
            data = self._get(path, params)
            items = data.get("MediaContainer", {}).get("Metadata", [])
            results.extend(items)
            total = int(data.get("MediaContainer", {}).get("totalSize", len(results)))
            start += len(items)
            if start >= total or not items:
                break
        return results

    # ------------------------------------------------------------------
    # Server / library discovery
    # ------------------------------------------------------------------

    def get_libraries(self) -> list:
        """Return all library sections on this server."""
        data = self._get("/library/sections")
        return data.get("MediaContainer", {}).get("Directory", [])

    # ------------------------------------------------------------------
    # Content fetching
    # ------------------------------------------------------------------

    def get_movies(self, library_id: str) -> list:
        """All movies in a library section."""
        return self._paginate(f"/library/sections/{library_id}/all",
                              {"type": 1})

    def get_shows(self, library_id: str) -> list:
        """All TV shows in a library section."""
        return self._paginate(f"/library/sections/{library_id}/all",
                              {"type": 2})

    def get_episodes(self, library_id: str) -> list:
        """All episodes in a library section (flat list)."""
        return self._paginate(f"/library/sections/{library_id}/all",
                              {"type": 4})

    def get_seasons(self, show_rating_key: str) -> list:
        data = self._get(f"/library/metadata/{show_rating_key}/children")
        return data.get("MediaContainer", {}).get("Metadata", [])

    def get_episodes_for_season(self, season_rating_key: str) -> list:
        data = self._get(f"/library/metadata/{season_rating_key}/children")
        return data.get("MediaContainer", {}).get("Metadata", [])

    def get_all_episodes_for_show(self, show_rating_key: str) -> list:
        """All episodes for a show (allLeaves endpoint)."""
        return self._paginate(f"/library/metadata/{show_rating_key}/allLeaves")

    def get_collections(self, library_id: str) -> list:
        return self._paginate(f"/library/sections/{library_id}/collections")

    def get_collection_items(self, collection_rating_key: str) -> list:
        data = self._get(f"/library/metadata/{collection_rating_key}/children")
        return data.get("MediaContainer", {}).get("Metadata", [])

    def get_recently_added(self, library_id: str) -> list:
        return self._paginate(f"/library/sections/{library_id}/recentlyAdded")

    def get_item_metadata(self, rating_key: str) -> Optional[dict]:
        data = self._get(f"/library/metadata/{rating_key}")
        items = data.get("MediaContainer", {}).get("Metadata", [])
        return items[0] if items else None

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def build_thumb_url(self, thumb_path: Optional[str]) -> Optional[str]:
        """Return a full URL for a Plex thumbnail path."""
        if not thumb_path:
            return None
        return f"{self.base_url}{thumb_path}?X-Plex-Token={self.token}"

    def get_stream_url(self, media_part_key: str) -> str:
        return f"{self.base_url}{media_part_key}?X-Plex-Token={self.token}"

    def get_server_info(self) -> dict:
        """Return basic server identity (machineIdentifier, friendlyName, etc.)."""
        data = self._get("/")
        return data.get("MediaContainer", {})

    def get_clients(self) -> list:
        """Return list of available Plex clients/players on this server."""
        data = self._get("/clients")
        return data.get("MediaContainer", {}).get("Server", [])

    def play_on_client(self, client_identifier: str, rating_key: str,
                       media_type: str, offset_ms: int = 0) -> bool:
        """Tell a Plex client to start playing a specific media item.

        Uses the Plex Media Player remote-control API.
        """
        # We need the server's machineIdentifier for the playMedia command
        server_info = self.get_server_info()
        machine_id = server_info.get("machineIdentifier", "")
        headers = {**PLEX_HEADERS, "X-Plex-Token": self.token}
        # Map type string to Plex type integer
        type_map = {"movie": 1, "episode": 4, "show": 2, "track": 10}
        plex_type = type_map.get(media_type, 1)
        params = {
            "machineIdentifier": machine_id,
            "key": f"/library/metadata/{rating_key}",
            "type": plex_type,
            "offset": offset_ms,
            "X-Plex-Token": self.token,
            "X-Plex-Target-Client-Identifier": client_identifier,
        }
        url = f"{self.base_url}/player/playback/playMedia"
        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=(7, 15))
            return resp.status_code < 400
        except Exception as exc:
            log.error("play_on_client failed: %s", exc)
            return False

    def verify_connection(self) -> bool:
        """Return True if we can reach the server AND the token is valid.

        /identity is unauthenticated on Plex — use /library/sections instead
        so a 401 (bad token) surfaces correctly.
        """
        try:
            self._get("/library/sections")
            return True
        except Exception:
            return False
