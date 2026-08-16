from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from game_collection import db
from game_collection.barcode_match import BarcodeCatalogEntry
from game_collection.cover_cache import CoverIndexEntry
from game_collection.providers import GameMatch
from game_collection.web import CollectionHandler, _apply_cover_entry_to_row, _fit_review_rows_to_expected_count, _layout


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


def multipart_body(
    filenames: list[str] | None = None,
    *,
    expected_titles: int = 1,
    file_field: str = "photos",
) -> tuple[bytes, str]:
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
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="expected_titles"\r\n\r\n'
            f"{expected_titles}\r\n"
        ).encode("utf-8"),
    ]
    for filename in filenames:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode("utf-8")
            + f"fake image bytes for {filename}\r\n".encode("utf-8")
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class WebIngestTests(unittest.TestCase):
    def test_layout_includes_ios_home_screen_metadata(self) -> None:
        page = _layout("Game Collection", "<h1>Library</h1>").decode("utf-8")

        self.assertIn('name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"', page)
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', page)
        self.assertIn('rel="manifest" href="/app.webmanifest"', page)
        self.assertIn('rel="apple-touch-icon" href="/apple-touch-icon.png"', page)
        self.assertIn("installResponsiveTables", page)
        self.assertIn("installServiceWorker", page)

    def test_app_assets_are_served_for_mobile_home_screen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.sqlite3"
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/app.webmanifest", timeout=10) as response:
                manifest = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.headers["Content-Type"], "application/manifest+json")

            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/apple-touch-icon.png", timeout=10) as response:
                icon = response.read()
                icon_type = response.headers["Content-Type"]

            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/service-worker.js", timeout=10) as response:
                worker = response.read().decode("utf-8")

        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(icon_type, "image/png")
        self.assertTrue(icon.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn("CACHE_NAME", worker)

    def test_fit_review_rows_to_expected_count_pads_and_trims(self) -> None:
        rows = [{"matched_title": "One"}, {"matched_title": "Two"}]

        padded = _fit_review_rows_to_expected_count(
            rows,
            expected_count=3,
            platform="PlayStation 5",
            play_status="completed",
        )
        trimmed = _fit_review_rows_to_expected_count(
            rows,
            expected_count=1,
            platform="PlayStation 5",
            play_status="completed",
        )

        self.assertEqual(len(padded), 3)
        self.assertEqual(padded[2]["platform"], "PlayStation 5")
        self.assertEqual(padded[2]["play_status"], "completed")
        self.assertEqual(padded[2]["decision"], "review")
        self.assertEqual(trimmed, [{"matched_title": "One"}])

    def test_library_browser_renders_swipe_shelves_and_metadata_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                metroid = db.upsert_game(
                    conn,
                    provider="igdb",
                    provider_game_id="metroid-prime",
                    title="Metroid Prime",
                    platform="Nintendo GameCube",
                    release_date="2002-11-17",
                    developer="Retro Studios",
                    publisher="Nintendo",
                    cover_url="https://example.test/metroid.jpg",
                )
                halo = db.upsert_game(
                    conn,
                    provider="igdb",
                    provider_game_id="halo-infinite",
                    title="Halo Infinite",
                    platform="Xbox Series X|S",
                    release_date="2021-12-08",
                    developer="343 Industries",
                    publisher="Xbox Game Studios",
                )
                db.add_collection_item(conn, game_id=metroid, acquisition_status="owned")
                db.add_playthrough(conn, game_id=metroid, play_status="completed")
                db.add_collection_item(conn, game_id=halo, acquisition_status="would_sell")
                db.add_playthrough(conn, game_id=halo, play_status="playing")

            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            body = handler._collection(handler, "")

        self.assertIn("library-shell", body)
        self.assertIn("Continue Playing", body)
        self.assertIn("Completed Archive", body)
        self.assertIn("Nintendo GameCube", body)
        self.assertIn("Xbox Game Studios", body)
        self.assertIn("2000s Releases", body)
        self.assertIn("2020s Releases", body)
        self.assertIn("https://example.test/metroid.jpg", body)
        self.assertNotIn('aria-label="Clear search and filters"', body)

    def test_library_browser_filters_by_platform_and_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                nintendo_game = db.upsert_game(
                    conn,
                    provider="igdb",
                    provider_game_id="mario",
                    title="Mario",
                    platform="Nintendo Switch",
                    publisher="Nintendo",
                )
                xbox_game = db.upsert_game(
                    conn,
                    provider="igdb",
                    provider_game_id="halo",
                    title="Halo",
                    platform="Xbox Series X|S",
                    publisher="Xbox Game Studios",
                )
                db.add_collection_item(conn, game_id=nintendo_game)
                db.add_collection_item(conn, game_id=xbox_game)

            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            query = urllib.parse.urlencode({"platform": "Nintendo Switch", "publisher": "Nintendo"})
            body = handler._collection(handler, query)

        self.assertIn("Mario", body)
        self.assertNotIn("Halo</div>", body)
        self.assertIn("Nintendo Switch", body)
        self.assertIn("filter-chip active", body)
        self.assertIn('href="/" aria-label="Clear search and filters"', body)
        self.assertIn("library-search-actions", body)
        self.assertIn("&#8634;", body)
        self.assertNotIn("&#10005;</span></a>", body)

    def test_layout_omits_separate_plan_navigation(self) -> None:
        page = _layout("Game Collection", "<h1>Library</h1>").decode("utf-8")

        self.assertIn('href="/" aria-label="Library"', page)
        self.assertIn('href="/ingest" aria-label="Scan a barcode"', page)
        self.assertIn('href="/caches" aria-label="Cache settings"', page)

    def test_library_search_actions_stay_inline_on_mobile(self) -> None:
        page = _layout("Game Collection", "<h1>Library</h1>").decode("utf-8")

        self.assertIn(
            ".library-search { grid-template-columns: minmax(0, 1fr) auto; }",
            page,
        )
        self.assertNotIn(".library-search { grid-template-columns: 1fr; }", page)
        self.assertNotIn('href="/plan"', page)

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
        self.assertNotIn('name="expected_titles"', body)
        self.assertNotIn("Expected titles", body)
        self.assertNotIn("Ownership status", body)
        self.assertNotIn("Initial play status", body)
        self.assertIn('name="photos"', body)
        self.assertIn('accept="image/*"', body)
        self.assertNotIn("multiple required", body)
        self.assertNotIn("Metadata provider", body)
        self.assertNotIn('name="provider"', body)
        self.assertNotIn('name="upload_mode"', body)
        self.assertNotIn('name="photo_library"', body)
        self.assertNotIn('name="camera_photo"', body)
        self.assertIn('data-role="platform-hint"', body)

    def test_upload_form_selects_last_platform_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": Path(tmp) / "collection.sqlite3"})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/ingest",
                headers={"Cookie": "game_collection_last_platform=PlayStation%205"},
            )
            with patch("game_collection.web.platform_cache_statuses") as statuses:
                statuses.return_value = [
                    type("Status", (), {"name": "Nintendo Switch", "cached": True, "count": 12})(),
                    type("Status", (), {"name": "PlayStation 5", "cached": True, "count": 9})(),
                ]
                with urllib.request.urlopen(request, timeout=10) as response:
                    body = response.read().decode("utf-8")

        self.assertIn('<option value="PlayStation 5" selected>PlayStation 5</option>', body)
        self.assertNotIn('capture="environment"', body)

    def test_upload_form_selects_platform_query_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": Path(tmp) / "collection.sqlite3"})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/ingest?platform=Nintendo%20Switch",
                headers={"Cookie": "game_collection_last_platform=PlayStation%205"},
            )
            with patch("game_collection.web.platform_cache_statuses") as statuses:
                statuses.return_value = [
                    type("Status", (), {"name": "Nintendo Switch", "cached": True, "count": 12})(),
                    type("Status", (), {"name": "PlayStation 5", "cached": True, "count": 9})(),
                ]
                with urllib.request.urlopen(request, timeout=10) as response:
                    body = response.read().decode("utf-8")

        self.assertIn('<option value="Nintendo Switch" selected>Nintendo Switch</option>', body)
        self.assertNotIn('<option value="PlayStation 5" selected>PlayStation 5</option>', body)

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
        self.assertIn("12 metadata rows", body)
        self.assertIn("Barcode Cache", body)
        self.assertIn('action="/barcode-sources"', body)
        self.assertIn('value="wikidata-video-games"', body)
        self.assertIn('value="csv-url"', body)
        self.assertIn('action="/library-art-refresh"', body)
        self.assertIn("Library Art", body)

    def test_game_detail_can_delete_collection_item_with_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                game_id = db.upsert_game(
                    conn,
                    provider="igdb",
                    provider_game_id="123",
                    title="Metroid Prime",
                    platform="Nintendo GameCube",
                    release_date="2002-11-17",
                    description="Explore Tallon IV.",
                )
                item_id = db.add_collection_item(conn, game_id=game_id)
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/games/{game_id}", timeout=10) as response:
                detail = response.read().decode("utf-8")

            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/items/{item_id}/delete",
                data=b"",
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                location = response.url
                redirected_body = response.read().decode("utf-8")

            with db.connect(db_path) as conn:
                rows = list(db.list_collection(conn))

        self.assertIn(f'action="/items/{item_id}/delete"', detail)
        self.assertIn("confirm('Delete this game from your library?", detail)
        self.assertIn('class="cover-stack"', detail)
        self.assertIn('class="metadata-list"', detail)
        self.assertNotIn("<h2>Collection Copy</h2>", detail)
        self.assertNotIn("<h2>Play History</h2>", detail)
        self.assertNotIn(f'action="/games/{game_id}/metadata"', detail)
        self.assertNotIn(f'action="/games/{game_id}/play"', detail)
        self.assertIn(f'action="/items/{item_id}/collection"', detail)
        self.assertNotIn("<h2>Metadata</h2>", detail)
        self.assertNotIn("<dt>Title</dt>", detail)
        self.assertNotIn("<dt>Platform</dt>", detail)
        self.assertNotIn("<dt>Developer</dt>", detail)
        self.assertNotIn("<dt>Publisher</dt>", detail)
        self.assertNotIn('name="title" value="Metroid Prime"', detail)
        self.assertNotIn("<dt>Cover URL</dt>", detail)
        self.assertIn("<dt>Release date</dt><dd>2002-11-17</dd>", detail)
        self.assertIn("<dt>Description</dt><dd>Explore Tallon IV.</dd>", detail)
        self.assertIn("Collection State", detail)
        self.assertIn("Played State", detail)
        self.assertNotIn("Condition notes", detail)
        self.assertNotIn("Sale notes", detail)
        self.assertNotIn("Location", detail)
        self.assertNotIn("Notes", detail)
        self.assertIn('<option value="owned" selected>Owned</option>', detail)
        self.assertIn('<option value="would_sell">Would Sell</option>', detail)
        self.assertIn('<option value="unplayed" selected>Unplayed</option>', detail)
        self.assertEqual(status, 200)
        self.assertTrue(location.endswith("/"))
        self.assertIn("0 shown from 0 collection item(s).", redirected_body)
        self.assertEqual(rows, [])

    def test_library_art_refresh_updates_games_missing_cover_art(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "collection.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                game_id = db.upsert_game(
                    conn,
                    provider="igdb",
                    provider_game_id="366878",
                    title="Super Mario Galaxy + Super Mario Galaxy 2",
                    platform="Nintendo Switch",
                    cover_url=None,
                )
                db.add_collection_item(conn, game_id=game_id)
            entry = CoverIndexEntry(
                provider="igdb",
                provider_game_id="366878",
                title="Super Mario Galaxy + Super Mario Galaxy 2",
                platform="Nintendo Switch",
                release_date="2025-10-02",
                developer="Nintendo Software Technology",
                publisher="Nintendo",
                description="Travel the stars with Mario.",
                cover_url="https://images.igdb.com/igdb/image/upload/t_cover_big/coavv6.jpg",
                cover_path=root / "covers" / "366878.jpg",
            )
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/library-art-refresh",
                data=b"",
                method="POST",
            )
            with (
                patch("game_collection.web.get_provider", return_value=FakeProvider()),
                patch("game_collection.web.find_or_fetch_cover_entry_for_title", return_value=entry) as refresh,
                patch("game_collection.web.platform_cache_statuses", return_value=[]),
            ):
                with urllib.request.urlopen(request, timeout=10) as response:
                    html = response.read().decode("utf-8")

            with db.connect(db_path) as conn:
                row = db.get_game_detail(conn, game_id=game_id)

        self.assertIn("updated 1", html)
        refresh.assert_called_once()
        self.assertEqual(row["cover_url"], "https://images.igdb.com/igdb/image/upload/t_cover_big/coavv6.jpg")

    def test_barcode_source_post_downloads_and_rebuilds_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": root / "collection.sqlite3"})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            form = urllib.parse.urlencode(
                {
                    "source": "upcdev-search",
                    "query": "Nintendo Switch",
                    "incremental": "1",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/barcode-sources",
                data=form,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with (
                patch("game_collection.web.BARCODE_SOURCE_ROOT", root / "barcode-sources"),
                patch("game_collection.web.download_upcdev_search", return_value=[object()]) as download,
                patch("game_collection.web.build_barcode_cache", return_value={"Nintendo Switch": 1, "all": 1}) as build,
                patch("game_collection.web.platform_cache_statuses") as statuses,
            ):
                statuses.return_value = [
                    type("Status", (), {"name": "Nintendo Switch", "cached": True, "count": 9})(),
                ]
                with urllib.request.urlopen(request, timeout=10) as response:
                    html = response.read().decode("utf-8")

        self.assertIn("Downloaded 1 source row", html)
        download.assert_called_once()
        build.assert_called_once()

    def test_review_table_shows_uploaded_image_and_cached_cover(self) -> None:
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
                        "barcode": "045496905651",
                        "source_provider": "wikidata",
                        "source_id": "Q1",
                        "provider": "igdb",
                        "provider_game_id": "123",
                        "matched_title": "Metroid Prime",
                        "confidence": "0.98",
                        "decision": "review",
                        "notes": "cover_path=review/cover-indexes/igdb/gamecube/covers/123.jpg",
                    }
                ],
            )

        self.assertIn("Uploaded", body)
        self.assertIn("Matched", body)
        self.assertIn("Barcode: 045496905651", body)
        self.assertNotIn("wikidata | Q1 | 045496905651", body)
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
        self.assertNotIn("<th>Play Status</th>", body)
        self.assertIn("Collection State", body)
        self.assertIn("Played State", body)
        self.assertIn('select name="row_0_acquisition_status"', body)
        self.assertIn('select name="row_0_play_status"', body)
        self.assertIn('<option value="owned" selected>Owned</option>', body)
        self.assertIn('<option value="would_sell">Would Sell</option>', body)
        self.assertIn('<option value="completed" selected>Completed</option>', body)
        self.assertIn('data-modal-image="/media?path=review/web-ingests/run/crops/upload-001-001.jpg"', body)
        self.assertIn('name="row_0_decision" value="accept"', body)
        self.assertIn('name="row_0_decision" value="ignore"', body)
        self.assertIn("Accept", body)
        self.assertIn("Reject", body)
        self.assertNotIn("Edit", body)
        self.assertNotIn("Again", body)
        self.assertNotIn('aria-label="Change match"', body)
        self.assertNotIn('aria-label="Scan another game"', body)
        self.assertNotIn("Review Queue", body)
        self.assertNotIn("Accepted", body)
        self.assertNotIn("Ignored", body)

    def test_apply_cover_entry_to_row_replaces_barcode_metadata(self) -> None:
        row = {
            "provider": "barcode",
            "provider_game_id": "045496905651",
            "candidate_title": "Super Mario Galaxy + Super Mario Galaxy 2",
            "matched_title": "Super Mario Galaxy + Super Mario Galaxy 2",
            "platform": "Nintendo Switch",
            "barcode": "045496905651",
            "notes": "barcode=045496905651; exact barcode catalog match",
        }
        entry = CoverIndexEntry(
            provider="igdb",
            provider_game_id="366878",
            title="Super Mario Galaxy + Super Mario Galaxy 2",
            platform="Nintendo Switch",
            release_date="2025-10-02",
            developer="Nintendo",
            publisher="Nintendo",
            description="Bundle",
            cover_url="https://images.igdb.com/igdb/image/upload/t_cover_big/example.jpg",
            cover_path=Path("review/cover-indexes/igdb/nintendo-switch/covers/366878.jpg"),
        )

        _apply_cover_entry_to_row(row, entry)

        self.assertEqual(row["provider"], "igdb")
        self.assertEqual(row["provider_game_id"], "366878")
        self.assertEqual(row["cover_url"], entry.cover_url)
        self.assertIn("cover_path=review/cover-indexes/igdb/nintendo-switch/covers/366878.jpg", row["notes"])

    def test_review_table_uses_cached_exact_title_cover_for_barcode_row(self) -> None:
        entry = CoverIndexEntry(
            provider="igdb",
            provider_game_id="366878",
            title="Super Mario Galaxy + Super Mario Galaxy 2",
            platform="Nintendo Switch",
            release_date="2025-10-02",
            developer="Nintendo",
            publisher="Nintendo",
            description="Bundle",
            cover_url="https://images.igdb.com/igdb/image/upload/t_cover_big/example.jpg",
            cover_path=Path("review/cover-indexes/igdb/nintendo-switch/covers/366878.jpg"),
        )
        with (
            patch("game_collection.web.platform_cache_statuses") as statuses,
            patch("game_collection.web._cached_cover_entries", return_value=[entry]),
        ):
            statuses.return_value = [
                type("Status", (), {"name": "Nintendo Switch", "cached": True, "count": 1})(),
            ]
            body = CollectionHandler._review_rows_table(
                CollectionHandler,
                [
                    {
                        "provider": "barcode",
                        "provider_game_id": "045496905651",
                        "matched_title": "Super Mario Galaxy + Super Mario Galaxy 2",
                        "platform": "Nintendo Switch",
                        "barcode": "045496905651",
                        "decision": "review",
                    }
                ],
            )

        self.assertIn("review/cover-indexes/igdb/nintendo-switch/covers/366878.jpg", body)
        self.assertIn('name="row_0_provider" value="igdb"', body)
        self.assertIn('name="row_0_provider_game_id" value="366878"', body)

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

    def test_single_review_card_renders_even_without_detected_rows(self) -> None:
        with patch("game_collection.web.platform_cache_statuses") as statuses:
            statuses.return_value = [
                type("Status", (), {"name": "PlayStation 5", "cached": True, "count": 9})(),
            ]
            body = CollectionHandler._review_rows_table(CollectionHandler, [])

        self.assertIn("single-review", body)
        self.assertIn('name="row_count" value="1"', body)
        self.assertIn('name="row_0_matched_title"', body)
        self.assertIn("No barcode match found", body)
        self.assertIn("Accept", body)
        self.assertNotIn("Manual Review", body)

    def test_ingest_results_shows_uploaded_photo_once_in_review_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run-1"
            uploads = run_dir / "uploads"
            uploads.mkdir(parents=True)
            (uploads / "upload-001.jpg").write_bytes(b"fake")
            (run_dir / "audit.csv").write_text(
                "photo_path,crop_path,candidate_title,platform,play_status,provider,provider_game_id,matched_title,release_date,developer,publisher,description,cover_url,confidence,decision,notes\n"
                "review/web-ingests/run-1/uploads/upload-001.jpg,review/web-ingests/run-1/uploads/upload-001.jpg,,PlayStation 5,unplayed,,,,,,,,,,review,\n",
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

        self.assertNotIn("Uploaded Photos", body)
        self.assertIn("upload-001.jpg", body)
        self.assertIn("single-review", body)

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
                        "provider,provider_game_id,title,platform,release_date,developer,publisher,description,cover_url,cover_path",
                        f"igdb,123,Metroid Prime,Nintendo GameCube,2002-11-17,Retro Studios,Nintendo,,https://example.test/cover.jpg,{covers_dir / '123.jpg'}",
                        f"igdb,456,Final Fantasy VII Remake,Nintendo GameCube,2020-04-10,Square Enix,Square Enix,,https://example.test/ff7.jpg,{covers_dir / '456.jpg'}",
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

    def test_accepting_ingest_review_redirects_to_scan_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "collection.sqlite3"
            run_id = "run-1"
            run_dir = root / run_id
            run_dir.mkdir()
            (run_dir / "audit.csv").write_text(
                "photo_path,crop_path,candidate_title,platform,play_status,barcode,source_provider,source_id,provider,provider_game_id,matched_title,release_date,developer,publisher,description,cover_url,confidence,decision,notes\n",
                encoding="utf-8",
            )
            (run_dir / "summary.csv").write_text("status,owned\nplayed,unplayed\n", encoding="utf-8")
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            form = urllib.parse.urlencode(
                {
                    "row_count": "1",
                    "row_0_provider": "igdb",
                    "row_0_provider_game_id": "123",
                    "row_0_matched_title": "Metroid Prime",
                    "row_0_platform": "Nintendo GameCube",
                    "row_0_decision": "accept",
                    "row_0_play_status": "unplayed",
                    "row_0_confidence": "1.00",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/ingest/{run_id}/review",
                data=form,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            opener = urllib.request.build_opener(NoRedirect)
            with patch("game_collection.web.WEB_INGEST_ROOT", root):
                try:
                    opener.open(request, timeout=10)
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    location = exc.headers["Location"]
                    cookie = exc.headers["Set-Cookie"]

            with db.connect(db_path) as conn:
                rows = list(db.list_collection(conn))

        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/ingest?message="))
        self.assertIn("game_collection_last_platform=Nintendo%20GameCube", cookie)
        self.assertEqual(len(rows), 1)

    def test_accepting_duplicate_ingest_is_blocked_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "collection.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                game_id = db.upsert_game(
                    conn,
                    provider="igdb",
                    provider_game_id="123",
                    title="Metroid Prime",
                    platform="Nintendo GameCube",
                )
                db.add_collection_item(conn, game_id=game_id)
                db.add_playthrough(conn, game_id=game_id, play_status="completed")
            run_id = "run-duplicate"
            run_dir = root / run_id
            run_dir.mkdir()
            (run_dir / "audit.csv").write_text(
                "photo_path,crop_path,candidate_title,platform,play_status,barcode,source_provider,source_id,provider,provider_game_id,matched_title,release_date,developer,publisher,description,cover_url,confidence,decision,notes\n",
                encoding="utf-8",
            )
            (run_dir / "summary.csv").write_text("status,owned\nplayed,unplayed\n", encoding="utf-8")
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            form = urllib.parse.urlencode(
                {
                    "row_count": "1",
                    "row_0_provider": "igdb",
                    "row_0_provider_game_id": "123",
                    "row_0_matched_title": "Metroid Prime",
                    "row_0_platform": "Nintendo GameCube",
                    "row_0_decision": "accept",
                    "row_0_play_status": "unplayed",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/ingest/{run_id}/review",
                data=form,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            opener = urllib.request.build_opener(NoRedirect)
            with patch("game_collection.web.WEB_INGEST_ROOT", root):
                try:
                    opener.open(request, timeout=10)
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    location = exc.headers["Location"]

            with db.connect(db_path) as conn:
                rows = list(db.list_collection(conn))

        self.assertEqual(status, 303)
        self.assertIn("Duplicate%20blocked", location)
        self.assertEqual(len(rows), 1)

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
                )
            ]

            def fake_detect(
                *,
                photo_path: Path,
                crops_dir: Path,
                platform: str | None = None,
                cover_entries: list[CoverIndexEntry] | None = None,
                barcode_entries=None,
                accept_threshold: float = 0.92,
                **kwargs,
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
                        "notes": "barcode=045496905651; exact barcode catalog match",
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
            self.assertIn("Review Game", html)
            self.assertIn("Accept", html)
            self.assertIn("Reject", html)
            self.assertNotIn("Again", html)
            with db.connect(db_path) as conn:
                rows = list(db.list_collection(conn))
            self.assertEqual(rows, [])

    def test_upload_ingest_assumes_single_game_even_with_legacy_expected_count(self) -> None:
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
                )
            ]

            def fake_detect(
                *,
                photo_path: Path,
                crops_dir: Path,
                platform: str | None = None,
                cover_entries: list[CoverIndexEntry] | None = None,
                barcode_entries=None,
                accept_threshold: float = 0.92,
                **kwargs,
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
                        "decision": "review",
                        "notes": "barcode=045496905651; exact barcode catalog match",
                    }
                ]

            body, content_type = multipart_body(expected_titles=3)
            url = f"http://127.0.0.1:{server.server_port}/ingest"
            request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
            with (
                patch("game_collection.web.WEB_INGEST_ROOT", root / "web-ingests"),
                patch("game_collection.web.read_cover_index", return_value=cover_entries),
                patch("game_collection.web.detect_photo_candidates", side_effect=fake_detect),
                patch("game_collection.web.get_provider", return_value=FakeProvider()),
                patch("game_collection.web.platform_cache_statuses") as statuses,
            ):
                statuses.return_value = [
                    type("Status", (), {"name": "Nintendo GameCube", "cached": True, "count": 9})(),
                ]
                with urllib.request.urlopen(request, timeout=10) as response:
                    html = response.read().decode("utf-8")

            self.assertIn("Review Game", html)
            self.assertNotIn("Detected barcodes:", html)
            self.assertIn('name="row_count" value="1"', html)
            self.assertIn('name="row_0_matched_title"', html)
            self.assertNotIn('name="row_1_matched_title"', html)
            self.assertNotIn('name="row_2_matched_title"', html)
            self.assertNotIn('name="row_3_matched_title"', html)
            self.assertNotIn("Expected title placeholder", html)

    def test_upload_ingest_passes_barcode_catalog_to_detector(self) -> None:
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
                )
            ]
            seen_catalog_sizes: list[int] = []
            seen_live_lookup: list[bool] = []

            def fake_detect(
                *,
                photo_path: Path,
                crops_dir: Path,
                platform: str | None = None,
                cover_entries: list[CoverIndexEntry] | None = None,
                barcode_entries=None,
                accept_threshold: float = 0.92,
                **kwargs,
            ):
                seen_catalog_sizes.append(len(barcode_entries or []))
                seen_live_lookup.append(bool(kwargs.get("live_lookup")))
                return []

            body, content_type = multipart_body()
            url = f"http://127.0.0.1:{server.server_port}/ingest"
            request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
            with (
                patch("game_collection.web.WEB_INGEST_ROOT", root / "web-ingests"),
                patch(
                    "game_collection.web.read_platform_barcode_cache",
                    return_value=[
                        BarcodeCatalogEntry(
                            barcode="045496905651",
                            title="Super Mario Galaxy + Super Mario Galaxy 2",
                            platform="Nintendo Switch",
                        )
                    ],
                ),
                patch("game_collection.web.read_cover_index", return_value=cover_entries),
                patch("game_collection.web.detect_photo_candidates", side_effect=fake_detect),
                patch("game_collection.web.get_provider", return_value=FakeProvider()),
                patch("game_collection.web.platform_cache_statuses") as statuses,
            ):
                statuses.return_value = [
                    type("Status", (), {"name": "Nintendo GameCube", "cached": True, "count": 9})(),
                ]
                with urllib.request.urlopen(request, timeout=10) as response:
                    response.read()

            self.assertEqual(seen_catalog_sizes, [1])
            self.assertEqual(seen_live_lookup, [True])

    def test_upload_ingest_remembers_selected_platform_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "collection.sqlite3"
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            def fake_detect(**kwargs):
                return []

            body, content_type = multipart_body()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/ingest",
                data=body,
                method="POST",
                headers={"Content-Type": content_type},
            )

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            opener = urllib.request.build_opener(NoRedirect)
            with (
                patch("game_collection.web.WEB_INGEST_ROOT", root / "web-ingests"),
                patch("game_collection.web.read_platform_barcode_cache", return_value=[]),
                patch("game_collection.web.read_cover_index", return_value=[]),
                patch("game_collection.web.detect_photo_candidates", side_effect=fake_detect),
                patch("game_collection.web.get_provider", return_value=FakeProvider()),
                patch("game_collection.web.platform_cache_statuses") as statuses,
            ):
                statuses.return_value = [
                    type("Status", (), {"name": "Nintendo GameCube", "cached": True, "count": 9})(),
                ]
                try:
                    opener.open(request, timeout=10)
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    location = exc.headers["Location"]
                    cookie = exc.headers["Set-Cookie"]

        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/ingest/"))
        self.assertIn("game_collection_last_platform=Nintendo%20GameCube", cookie)

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
                )
            ]

            def fake_detect(
                *,
                photo_path: Path,
                crops_dir: Path,
                platform: str | None = None,
                cover_entries: list[CoverIndexEntry] | None = None,
                barcode_entries=None,
                accept_threshold: float = 0.92,
                **kwargs,
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
                        "notes": "barcode=045496905651; exact barcode catalog match",
                    }
                ]

            body, content_type = multipart_body(["one.jpg", "two.jpg", "three.jpg"], expected_titles=3)
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

            self.assertIn("Review Game", html)
            self.assertIn('name="row_count" value="1"', html)
            self.assertIn('name="row_0_matched_title"', html)
            self.assertNotIn('name="row_1_matched_title"', html)
            self.assertEqual(detect.call_count, 3)
            with db.connect(db_path) as conn:
                titles = [row["title"] for row in db.list_collection(conn)]
            self.assertEqual(titles, [])

    def test_upload_ingest_accepts_camera_photo_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "collection.sqlite3"
            handler = type("TestCollectionHandler", (CollectionHandler,), {"db_path": db_path})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            seen_uploads: list[str] = []

            def fake_detect(
                *,
                photo_path: Path,
                crops_dir: Path,
                platform: str | None = None,
                cover_entries: list[CoverIndexEntry] | None = None,
                barcode_entries=None,
                accept_threshold: float = 0.92,
                **kwargs,
            ):
                seen_uploads.append(photo_path.name)
                return []

            body, content_type = multipart_body(["camera.jpg"], file_field="camera_photo")
            url = f"http://127.0.0.1:{server.server_port}/ingest"
            request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
            with (
                patch("game_collection.web.WEB_INGEST_ROOT", root / "web-ingests"),
                patch("game_collection.web.read_platform_barcode_cache", return_value=[]),
                patch("game_collection.web.read_cover_index", return_value=[]),
                patch("game_collection.web.detect_photo_candidates", side_effect=fake_detect),
                patch("game_collection.web.get_provider", return_value=FakeProvider()),
                patch("game_collection.web.platform_cache_statuses") as statuses,
            ):
                statuses.return_value = [
                    type("Status", (), {"name": "Nintendo GameCube", "cached": True, "count": 9})(),
                ]
                with urllib.request.urlopen(request, timeout=10) as response:
                    html = response.read().decode("utf-8")

            self.assertIn("Ingest Results", html)
            self.assertEqual(seen_uploads, ["upload-001.jpg"])


if __name__ == "__main__":
    unittest.main()
