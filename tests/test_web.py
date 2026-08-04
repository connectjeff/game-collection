from __future__ import annotations

import json
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

    def test_review_table_compares_uploaded_crop_to_matched_cover(self) -> None:
        with patch("game_collection.web.platform_cache_statuses") as statuses:
            statuses.return_value = [
                type("Status", (), {"name": "Nintendo GameCube", "cached": True, "count": 12})(),
                type("Status", (), {"name": "PlayStation 5", "cached": True, "count": 9})(),
            ]
            body = CollectionHandler._review_rows_table(
                CollectionHandler,
                [
                    {
                        "photo_path": "review/web-ingests/run/uploads/upload-001.jpg",
                        "crop_path": "review/web-ingests/run/crops/upload-001-001.jpg",
                        "candidate_title": "Metroid Prime",
                        "platform": "Nintendo GameCube",
                        "play_status": "completed",
                        "provider": "igdb",
                        "provider_game_id": "123",
                        "matched_title": "Metroid Prime",
                        "confidence": "0.98",
                        "decision": "review",
                        "notes": "cover_match_distance=1; cover_path=review/cover-indexes/igdb/gamecube/covers/123.jpg",
                    }
                ],
            )

        self.assertIn("Uploaded", body)
        self.assertIn("Matched", body)
        self.assertIn("review/cover-indexes/igdb/gamecube/covers/123.jpg", body)
        self.assertNotIn("<th>Notes</th>", body)
        self.assertNotIn("<th>Provider</th>", body)
        self.assertNotIn("<th>Provider ID</th>", body)
        self.assertNotIn("<th>Conf.</th>", body)
        self.assertNotIn("<select name=\"row_0_decision\"", body)
        self.assertIn('data-role="match-title-input"', body)
        self.assertIn('<textarea class="title-field" name="row_0_matched_title"', body)
        self.assertNotIn('name="row_0_candidate_title" rows=', body)
        self.assertIn('type="hidden" name="row_0_candidate_title"', body)
        self.assertIn('type="hidden" name="row_0_notes"', body)
        self.assertIn('type="hidden" name="row_0_provider"', body)
        self.assertIn('type="hidden" name="row_0_confidence"', body)
        self.assertIn('select name="row_0_platform"', body)
        self.assertIn('value="Nintendo GameCube" selected', body)
        self.assertIn('value="PlayStation 5"', body)
        self.assertIn("<th>Play Status</th>", body)
        self.assertIn('select name="row_0_play_status"', body)
        self.assertIn('value="completed" selected', body)
        self.assertIn('data-modal-image="/media?path=review/web-ingests/run/crops/upload-001-001.jpg"', body)
        self.assertIn('data-action-decision="accept"', body)
        self.assertIn('data-action-decision="ignore"', body)
        self.assertIn('data-role="decision-actions"', body)
        self.assertNotIn('data-action-decision="review"', body)
        self.assertIn("Review Queue", body)
        self.assertIn("Accepted", body)
        self.assertIn("Ignored", body)

    def test_review_platform_selector_preserves_uncached_current_platform(self) -> None:
        with patch("game_collection.web.platform_cache_statuses") as statuses:
            statuses.return_value = [
                type("Status", (), {"name": "PlayStation 5", "cached": True, "count": 9})(),
            ]
            body = CollectionHandler._review_rows_table(
                CollectionHandler,
                [
                    {
                        "platform": "Nintendo GameCube",
                        "matched_title": "Metroid Prime",
                        "decision": "review",
                    }
                ],
            )

        self.assertIn('select name="row_0_platform"', body)
        self.assertIn('value="Nintendo GameCube" selected', body)
        self.assertIn('value="PlayStation 5"', body)

    def test_manual_review_section_renders_even_without_detected_rows(self) -> None:
        with patch("game_collection.web.platform_cache_statuses") as statuses:
            statuses.return_value = [
                type("Status", (), {"name": "PlayStation 5", "cached": True, "count": 9})(),
            ]
            body = CollectionHandler._review_rows_table(CollectionHandler, [])

        self.assertIn("Manual Review", body)
        self.assertIn('data-role="manual-platform"', body)
        self.assertIn('data-role="manual-title"', body)
        self.assertIn('data-role="manual-add"', body)
        self.assertIn('name="row_count" value="0"', body)
        self.assertIn("Review Queue", body)
        self.assertIn("No rows waiting for review.", body)

    def test_ingest_results_shows_uploaded_photo_thumbnails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run-1"
            uploads = run_dir / "uploads"
            uploads.mkdir(parents=True)
            (uploads / "upload-001.jpg").write_bytes(b"fake")
            (run_dir / "audit.csv").write_text(
                "photo_path,crop_path,candidate_title,platform,play_status,provider,provider_game_id,matched_title,release_date,developer,publisher,description,cover_url,confidence,decision,notes\n",
                encoding="utf-8",
            )
            (run_dir / "summary.csv").write_text("provider,igdb\nplatform,PlayStation 5\n", encoding="utf-8")

            with patch("game_collection.web.WEB_INGEST_ROOT", root):
                with patch("game_collection.web.platform_cache_statuses") as statuses:
                    statuses.return_value = [
                        type("Status", (), {"name": "PlayStation 5", "cached": True, "count": 9})(),
                    ]
                    handler = object.__new__(CollectionHandler)
                    body = handler._ingest_results("run-1")

        self.assertIn("Uploaded Photos", body)
        self.assertIn("upload-001.jpg", body)
        self.assertIn("Manual Review", body)

    def test_review_outcome_rows_have_section_specific_actions(self) -> None:
        body = CollectionHandler._review_rows_table(
            CollectionHandler,
            [
                {
                    "photo_path": "photo.jpg",
                    "crop_path": "crop.jpg",
                    "candidate_title": "Accepted Game",
                    "platform": "PlayStation 5",
                    "provider": "igdb",
                    "provider_game_id": "1",
                    "matched_title": "Accepted Game",
                    "decision": "accept",
                },
                {
                    "photo_path": "photo.jpg",
                    "crop_path": "crop.jpg",
                    "candidate_title": "Ignored Game",
                    "platform": "PlayStation 5",
                    "provider": "igdb",
                    "provider_game_id": "2",
                    "matched_title": "Ignored Game",
                    "decision": "ignore",
                },
            ],
        )

        accepted_row = body[body.index('data-row="0"'):body.index('data-row="1"')]
        ignored_row = body[body.index('data-row="1"'):]
        self.assertIn('data-action-decision="review"', accepted_row)
        self.assertIn('data-action-decision="ignore"', accepted_row)
        self.assertNotIn('data-action-decision="accept"', accepted_row)
        self.assertIn('data-action-decision="review"', ignored_row)
        self.assertIn('data-action-decision="accept"', ignored_row)
        self.assertNotIn('data-action-decision="ignore"', ignored_row)

    def test_match_search_returns_cached_cover_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "review" / "cover-indexes" / "igdb" / "nintendo-gamecube" / "index.csv"
            covers_dir = index_path.parent / "covers"
            covers_dir.mkdir(parents=True)
            (covers_dir / "123.jpg").write_bytes(b"fake")
            (covers_dir / "456.jpg").write_bytes(b"fake")
            index_path.write_text(
                "\n".join(
                    [
                        "provider,provider_game_id,title,platform,release_date,developer,publisher,description,cover_url,cover_path,phash",
                        f"igdb,123,Metroid Prime,Nintendo GameCube,2002-11-17,Retro Studios,Nintendo,,https://example.test/cover.jpg,{covers_dir / '123.jpg'},abc",
                        f"igdb,456,Final Fantasy VII Remake,Nintendo GameCube,2020-04-10,Square Enix,Square Enix,,https://example.test/ff7.jpg,{covers_dir / '456.jpg'},def",
                    ]
                ),
                encoding="utf-8",
            )
            db_path = root / "collection.sqlite3"
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            url = f"http://127.0.0.1:{server.server_port}/matches?platform=Nintendo%20GameCube&q=metro"
            with patch("game_collection.web.default_index_path", return_value=index_path):
                with urllib.request.urlopen(url, timeout=10) as response:
                    payload = response.read().decode("utf-8")

            self.assertIn('"title": "Metroid Prime"', payload)
            self.assertIn('"provider_game_id": "123"', payload)
            self.assertIn('"cover_path":', payload)

            url = f"http://127.0.0.1:{server.server_port}/matches?platform=Nintendo%20GameCube&q=final%20remake"
            with patch("game_collection.web.default_index_path", return_value=index_path):
                with urllib.request.urlopen(url, timeout=10) as response:
                    matches = json.loads(response.read().decode("utf-8"))

            self.assertEqual(matches[0]["title"], "Final Fantasy VII Remake")
            self.assertEqual(matches[0]["provider_game_id"], "456")

    def test_upload_ingest_creates_manual_review_suggestions_without_importing(self) -> None:
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
            self.assertIn("suggested match", html)
            with db.connect(db_path) as conn:
                rows = list(db.list_collection(conn))
            self.assertEqual(rows, [])

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

            self.assertIn("<strong>3</strong> suggested match", html)
            self.assertEqual(detect.call_count, 3)
            with db.connect(db_path) as conn:
                titles = [row["title"] for row in db.list_collection(conn)]
            self.assertEqual(titles, [])


if __name__ == "__main__":
    unittest.main()
