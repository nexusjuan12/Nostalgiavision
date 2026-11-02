"""Regression checks for serving the UI below a reverse-proxy subpath."""

import re
import unittest
from urllib.parse import urljoin
from unittest.mock import patch

import app
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.test import Client
from werkzeug.wrappers import Response


class SubpathTests(unittest.TestCase):
    def test_guide_uses_its_document_url_for_app_requests(self):
        response = app.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("const APP_BASE = new URL('.', document.baseURI);", html)
        self.assertIn('src="image/1.png"', html)
        self.assertIn("url('image/test-pattern.png')", html)
        self.assertNotRegex(html, r"(?<!\w)fetch\s*\(\s*(?!appUrl\()")
        self.assertNotRegex(html, r'\b(?:src|href)="/(?!/)')

        # The same app-relative paths work at both the domain root and /ntv/.
        for base in ("https://example.com/", "https://example.com/ntv/"):
            self.assertEqual(urljoin(base, "api/status"), base + "api/status")
            self.assertEqual(urljoin(base, "image/1.png"), base + "image/1.png")

        mounted = Client(
            DispatcherMiddleware(Response("Not found", status=404), {"/ntv": app.app}),
            Response,
        )
        self.assertEqual(mounted.get("/ntv/").status_code, 200)
        asset = mounted.get("/ntv/image/1.png")
        try:
            self.assertEqual(asset.status_code, 200)
        finally:
            asset.close()

    def test_player_poster_and_back_link_stay_under_subpath(self):
        class PlexStub:
            def get_item_metadata(self, _rating_key):
                return {"title": "Example", "thumb": "/library/metadata/1/thumb"}

        with patch.object(app, "_load_config", return_value={
            "plex_url": "https://plex.example", "plex_token": "test-token",
        }), patch.object(app, "_get_client", return_value=PlexStub()):
            response = app.app.test_client().get("/watch/1")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        poster = re.search(r'<img id="poster" src="([^"]+)"', html)
        self.assertIsNotNone(poster)
        self.assertEqual(
            urljoin("https://example.com/ntv/watch/1", poster.group(1)).split("?")[0],
            "https://example.com/ntv/api/thumb",
        )
        self.assertIn('<a href="../">Back to guide</a>', html)
        self.assertEqual(
            urljoin("https://example.com/ntv/watch/1", "../"),
            "https://example.com/ntv/",
        )


if __name__ == "__main__":
    unittest.main()
