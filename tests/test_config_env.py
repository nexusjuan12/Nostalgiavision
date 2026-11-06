"""Environment-variable overrides for config.json (used by the Docker image)."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app
import database as db

ENV_VARS = list(app._ENV_OVERRIDES)


class ConfigEnvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = os.path.join(self._tmp.name, "data", "config.json")  # dir doesn't exist yet
        # Start from a clean environment so the host's own variables can't leak in.
        clean = {k: v for k, v in os.environ.items() if k not in ENV_VARS}
        for p in (patch.dict(os.environ, clean, clear=True),
                  patch.object(app, "CONFIG_PATH", self.config_path),
                  patch.object(db, "DB_PATH", os.path.join(self._tmp.name, "t.db"))):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        self.client = app.app.test_client()

    def _write_config(self, cfg):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(cfg, f)

    def _on_disk(self):
        with open(self.config_path) as f:
            return json.load(f)

    def test_config_file_is_optional(self):
        self.assertEqual(app._load_config(), {})
        self.assertIsNone(app._get_client())

    def test_env_alone_configures_plex_and_libraries(self):
        os.environ.update(PLEX_URL="http://plex.lan:32400/", PLEX_TOKEN="tok",
                          LIBRARY_IDS="1, 2,3")
        cfg = app._load_config()
        self.assertEqual(cfg["library_ids"], ["1", "2", "3"])
        client = app._get_client()
        self.assertEqual(client.base_url, "http://plex.lan:32400")
        self.assertEqual(client.token, "tok")

    def test_env_overrides_file_and_blank_env_does_not(self):
        self._write_config({"plex_url": "http://file:32400", "plex_token": "file-tok",
                            "schedule_days_ahead": 3})
        os.environ.update(PLEX_URL="http://env:32400", PLEX_TOKEN="  ",
                          SCHEDULE_DAYS_AHEAD="7")
        cfg = app._load_config()
        self.assertEqual(cfg["plex_url"], "http://env:32400")
        self.assertEqual(cfg["plex_token"], "file-tok")        # blank → file value kept
        self.assertEqual(cfg["schedule_days_ahead"], 7)

    def test_invalid_number_is_ignored(self):
        self._write_config({"schedule_days_ahead": 3})
        os.environ["SCHEDULE_DAYS_AHEAD"] = "soon"
        self.assertEqual(app._load_config()["schedule_days_ahead"], 3)

    def test_host_and_port_come_from_prefixed_env(self):
        self._write_config({"host": "127.0.0.1", "port": 8080})
        os.environ.update(NOSTALGIAVISION_HOST="0.0.0.0", NOSTALGIAVISION_PORT="5000")
        cfg = app._load_config()
        self.assertEqual((cfg["host"], cfg["port"]), ("0.0.0.0", 5000))

    def test_env_values_are_never_written_to_config_file(self):
        self._write_config({"plex_url": "http://file:32400"})
        os.environ.update(PLEX_TOKEN="secret", LIBRARY_IDS="9")
        # /api/sync persists the chosen libraries via read-modify-write of the file.
        with patch.object(app, "_get_client", return_value=None):
            resp = self.client.post("/api/sync", json={"library_ids": ["4"]})
        self.assertEqual(resp.status_code, 401)                # no Plex client → stops after saving
        on_disk = self._on_disk()
        self.assertEqual(on_disk["library_ids"], ["4"])
        self.assertNotIn("plex_token", on_disk)
        self.assertEqual(app._load_config()["library_ids"], ["9"])   # env still wins when read

    def test_config_file_created_on_save_when_missing(self):
        with patch.object(app, "_get_client", return_value=None):
            self.client.post("/api/sync", json={"library_ids": ["2"]})
        self.assertEqual(self._on_disk(), {"library_ids": ["2"]})

    def test_connect_refused_when_plex_settings_come_from_env(self):
        os.environ["PLEX_URL"] = "http://env:32400"
        resp = self.client.post("/api/connect",
                                json={"plex_url": "http://other:32400", "plex_token": "t"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("PLEX_URL", resp.get_json()["error"])
        self.assertFalse(os.path.exists(self.config_path))

    def test_status_reports_effective_library_ids(self):
        os.environ["LIBRARY_IDS"] = "4,5"
        app._status_response_cache["data"] = None              # bypass the 5s response cache
        self.assertEqual(self.client.get("/api/status").get_json()["library_ids"], ["4", "5"])


if __name__ == "__main__":
    unittest.main()
