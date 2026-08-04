from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from game_collection import db
from game_collection.cover_match import CoverIndexEntry
from game_collection.providers import GameMatch
from game_collection.web import CollectionHandler


class FakeProvider:
    name = "fake"

    def search(self, title: str, platform: str | None = None, limit: int = 5) -> list[GameMatch]:
        return [
            GameMatch(
                provider="fake",
                provider_game_id=title.casefold().replace(" ", "-"),
                title=title,
                platform=platform,
                confidence=0.98,
                raw={"source": "fake"},
            )
        ]


def multipart_body(filenames: list[str] | None = None) -> tuple[bytes, str]:
    boundary = "----gamecollectiontest"
    filenames = filenames or ["games.jpg"]
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
    ]
    for filename in filenames:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="photos"; filename="{filename}"\r\n'
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode("utf-8")
            + f"fake image bytes for {filename}\r\n".encode("utf-8")
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class WebIngestTests(unittest.TestCase):
    def test_upload_form_includes_only_cached_platforms(self) -> None:
        with patch("game_collection.web.platform_cache_statuses") as statuses:
            statuses.return_value = [
                type("Status", (), {"name": "PlayStation 5", "cached": True, "count": 12})(),
                type("Status", (), {"name": "PlayStation 4", "cached": False, "count": 0})(),
            ]
            body = CollectionHandler._ingest_form(CollectionHandler)

        self.assertIn('<option value="PlayStation 5">PlayStation 5</option>', body)
        self.assertNotIn('<option value="PlayStation 4">PlayStation 4</option>', body)
        self.assertNotIn("Cover index limit", body)

    def test_cache_settings_page_lists_platform_checkboxes(self) -> None:
        handler = type(
            "TestCollectionHandler",
            (CollectionHandler,),
            {"platform_options": ["PlayStation 5", "PlayStation 4"]},
        )

        with patch("game_collection.web.platform_cache_statuses") as statuses:
            statuses.return_value = [
                type("Status", (), {"name": "PlayStation 5", "cached": True, "count": 12})(),
                type("Status", (), {"name": "PlayStation 4", "cached": False, "count": 0})(),
            ]
            body = handler._cache_settings(handler)

        self.assertIn('name="platform" value="PlayStation 5" checked', body)
        self.assertIn('name="platform" value="PlayStation 4"', body)
        self.assertIn("12 covers", body)

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

            cover_entries = [
                CoverIndexEntry(
                    provider="fake",
                    provider_game_id="metroid-prime",
                    title="Metroid Prime",
                    platform="Nintendo GameCube",
                    release_date=None,
                    developer=None,
                    publisher=None,
                    description=None,
                    cover_url=None,
                    cover_path=root / "cover.jpg",
                    phash="0",
                )
            ]

            def fake_detect(
                *,
                photo_path: Path,
                crops_dir: Path,
                platform: str | None = None,
                cover_entries: list[CoverIndexEntry] | None = None,
                accept_threshold: float = 0.92,
            ):
                return [
                    {
                        "photo_path": str(photo_path),
                        "crop_path": "",
                        "candidate_title": "Metroid Prime",
                        "platform": platform or "",
                        "provider": "fake",
                        "provider_game_id": "metroid-prime",
                        "matched_title": "Metroid Prime",
                        "release_date": "",
                        "developer": "",
                        "publisher": "",
                        "description": "",
                        "cover_url": "",
                        "confidence": "0.98",
                        "decision": "accept",
                        "notes": "cover_match_distance=1",
                    }
                ]

            body, content_type = multipart_body()
            url = f"http://127.0.0.1:{server.server_port}/ingest"
            request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
            with (
                patch("game_collection.web.WEB_INGEST_ROOT", root / "web-ingests"),
                patch("game_collection.web.read_cover_index", return_value=cover_entries),
                patch("game_collection.web.detect_photo_candidates", side_effect=fake_detect),
                patch("game_collection.web.get_provider", return_value=FakeProvider()),
            ):
                with urllib.request.urlopen(request, timeout=10) as response:
                    html = response.read().decode("utf-8")

            self.assertIn("Ingest Results", html)
            with db.connect(db_path) as conn:
                rows = list(db.list_collection(conn))
            self.assertEqual([row["title"] for row in rows], ["Metroid Prime"])

    def test_upload_ingest_handles_multiple_images_in_one_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "collection.sqlite3"
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            cover_entries = [
                CoverIndexEntry(
                    provider="fake",
                    provider_game_id="placeholder",
                    title="Placeholder",
                    platform="Nintendo GameCube",
                    release_date=None,
                    developer=None,
                    publisher=None,
                    description=None,
                    cover_url=None,
                    cover_path=root / "cover.jpg",
                    phash="0",
                )
            ]

            def fake_detect(
                *,
                photo_path: Path,
                crops_dir: Path,
                platform: str | None = None,
                cover_entries: list[CoverIndexEntry] | None = None,
                accept_threshold: float = 0.92,
            ):
                title = {
                    "upload-001.jpg": "Metroid Prime",
                    "upload-002.jpg": "F-Zero GX",
                    "upload-003.jpg": "Pikmin 2",
                }[photo_path.name]
                return [
                    {
                        "photo_path": str(photo_path),
                        "crop_path": "",
                        "candidate_title": title,
                        "platform": platform or "",
                        "provider": "fake",
                        "provider_game_id": title.casefold().replace(" ", "-"),
                        "matched_title": title,
                        "release_date": "",
                        "developer": "",
                        "publisher": "",
                        "description": "",
                        "cover_url": "",
                        "confidence": "0.98",
                        "decision": "accept",
                        "notes": "cover_match_distance=1",
                    }
                ]

            body, content_type = multipart_body(["one.jpg", "two.jpg", "three.jpg"])
            url = f"http://127.0.0.1:{server.server_port}/ingest"
            request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
            with (
                patch("game_collection.web.WEB_INGEST_ROOT", root / "web-ingests"),
                patch("game_collection.web.read_cover_index", return_value=cover_entries),
                patch("game_collection.web.detect_photo_candidates", side_effect=fake_detect) as detect,
                patch("game_collection.web.get_provider", return_value=FakeProvider()),
            ):
                with urllib.request.urlopen(request, timeout=10) as response:
                    html = response.read().decode("utf-8")

            self.assertIn("<strong>3</strong> imported", html)
            self.assertEqual(detect.call_count, 3)
            with db.connect(db_path) as conn:
                titles = [row["title"] for row in db.list_collection(conn)]
            self.assertEqual(titles, ["F-Zero GX", "Metroid Prime", "Pikmin 2"])


if __name__ == "__main__":
    unittest.main()
