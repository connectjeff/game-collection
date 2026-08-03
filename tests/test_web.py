from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from game_collection import db
from game_collection.providers import GameMatch
from game_collection.web import CollectionHandler


class FakeProvider:
    name = "fake"

    def search(self, title: str, platform: str | None = None, limit: int = 5) -> list[GameMatch]:
        return [
            GameMatch(
                provider="fake",
                provider_game_id="metroid-prime",
                title="Metroid Prime",
                platform=platform,
                confidence=0.98,
                raw={"source": "fake"},
            )
        ]


def multipart_body() -> tuple[bytes, str]:
    boundary = "----gamecollectiontest"
    parts = [
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="provider"\r\n\r\n'
            "igdb\r\n"
        ).encode("utf-8"),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="platform"\r\n\r\n'
            "Nintendo GameCube\r\n"
        ).encode("utf-8"),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="accept_threshold"\r\n\r\n'
            "0.92\r\n"
        ).encode("utf-8"),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="photos"; filename="games.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8")
        + b"fake image bytes\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class WebIngestTests(unittest.TestCase):
    def test_upload_ingest_imports_high_confidence_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "collection.sqlite3"
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            def fake_detect(*, photo_path: Path, crops_dir: Path, platform: str | None = None):
                return [
                    {
                        "photo_path": str(photo_path),
                        "crop_path": "",
                        "candidate_title": "Metroid Prime",
                        "platform": platform or "",
                        "provider": "",
                        "provider_game_id": "",
                        "matched_title": "",
                        "release_date": "",
                        "developer": "",
                        "publisher": "",
                        "description": "",
                        "cover_url": "",
                        "confidence": "",
                        "decision": "review",
                        "notes": "",
                    }
                ]

            body, content_type = multipart_body()
            url = f"http://127.0.0.1:{server.server_port}/ingest"
            request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
            with (
                patch("game_collection.web.WEB_INGEST_ROOT", root / "web-ingests"),
                patch("game_collection.web.detect_photo_candidates", side_effect=fake_detect),
                patch("game_collection.web.get_provider", return_value=FakeProvider()),
            ):
                with urllib.request.urlopen(request, timeout=10) as response:
                    html = response.read().decode("utf-8")

            self.assertIn("Ingest Results", html)
            with db.connect(db_path) as conn:
                rows = list(db.list_collection(conn))
            self.assertEqual([row["title"] for row in rows], ["Metroid Prime"])


if __name__ == "__main__":
    unittest.main()

