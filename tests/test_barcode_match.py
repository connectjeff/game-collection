from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_collection.barcode_match import (
    BarcodeCatalogEntry,
    build_barcode_cache,
    barcode_variants,
    detect_barcodes,
    is_valid_gtin,
    is_expected_barcode_for_platform,
    match_barcode,
    normalize_barcode,
    platform_barcode_hint,
    read_barcode_catalog,
    write_barcode_catalog,
)
from game_collection.cover_cache import CoverIndexEntry


ATTACHED_BARCODE_PHOTO = Path(
    "/tmp/codex-remote-attachments/019fc877-3158-7d70-aa85-40611301ec25/"
    "389255A9-7923-426A-9F3C-7BE34E100355/1-Photo-1.jpg"
)


class BarcodeMatchTests(unittest.TestCase):
    def test_normalize_barcode_handles_nintendo_upc_forms(self) -> None:
        self.assertEqual(normalize_barcode("0 45496 90565 1"), "045496905651")
        self.assertEqual(normalize_barcode("00045496905651"), "045496905651")
        self.assertIn("00045496905651", barcode_variants("045496905651"))

    def test_platform_hint_filters_retail_barcode_lengths_and_prefers_known_prefixes(self) -> None:
        switch_hint = platform_barcode_hint("Nintendo Switch")
        ps5_hint = platform_barcode_hint("PlayStation 5")
        xbox_hint = platform_barcode_hint("Xbox Series X|S")

        self.assertIn("045496", switch_hint.preferred_prefixes)
        self.assertIn("711719", ps5_hint.preferred_prefixes)
        self.assertIn("889842", xbox_hint.preferred_prefixes)
        self.assertTrue(is_expected_barcode_for_platform("045496905651", "Nintendo Switch"))
        self.assertTrue(is_expected_barcode_for_platform("812303012341", "Nintendo Switch"))
        self.assertFalse(is_expected_barcode_for_platform("00000", "Nintendo Switch"))
        self.assertFalse(is_expected_barcode_for_platform("045496905650", "Nintendo Switch"))

    def test_gtin_checksum_validation_accepts_standard_retail_lengths(self) -> None:
        self.assertTrue(is_valid_gtin("045496905651"))
        self.assertTrue(is_valid_gtin("00045496905651"))
        self.assertFalse(is_valid_gtin("123456789013"))

    def test_read_and_match_barcode_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.csv"
            write_barcode_catalog(
                path,
                [
                    BarcodeCatalogEntry(
                        barcode="045496905651",
                        title="Super Mario Galaxy + Super Mario Galaxy 2",
                        platform="Nintendo Switch",
                    )
                ],
            )

            catalog = read_barcode_catalog(path)
            match = match_barcode("00045496905651", catalog, platform="Nintendo Switch")

        self.assertIsNotNone(match)
        self.assertEqual(match.title, "Super Mario Galaxy + Super Mario Galaxy 2")
        self.assertEqual(match.provider, "barcode")
        self.assertEqual(match.provider_game_id, "045496905651")
        self.assertEqual(match.confidence, 1.0)

    def test_match_barcode_uses_cover_index_metadata_when_available(self) -> None:
        catalog = [
            BarcodeCatalogEntry(
                barcode="045496905651",
                title="Super Mario Galaxy + Super Mario Galaxy 2",
                platform="Nintendo Switch",
            )
        ]
        cover_entries = [
            CoverIndexEntry(
                provider="igdb",
                provider_game_id="12345",
                title="Super Mario Galaxy + Super Mario Galaxy 2",
                platform="Nintendo Switch",
                release_date="2025-10-02",
                developer="Nintendo",
                publisher="Nintendo",
                description=None,
                cover_url="https://example.test/cover.jpg",
                cover_path=Path("cover.jpg"),
            )
        ]

        match = match_barcode("045496905651", catalog, platform="Nintendo Switch", cover_entries=cover_entries)

        self.assertIsNotNone(match)
        self.assertEqual(match.provider, "igdb")
        self.assertEqual(match.provider_game_id, "12345")
        self.assertEqual(match.release_date, "2025-10-02")
        self.assertEqual(match.cover_url, "https://example.test/cover.jpg")

    def test_builds_platform_barcode_cache_from_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            cache_root = root / "barcodes"
            write_barcode_catalog(
                source,
                [
                    BarcodeCatalogEntry(
                        barcode="045496905651",
                        title="Super Mario Galaxy + Super Mario Galaxy 2",
                        platform="Nintendo Switch",
                    ),
                    BarcodeCatalogEntry(
                        barcode="711719541028",
                        title="Example PlayStation Game",
                        platform="PlayStation 5",
                    ),
                ],
            )

            results = build_barcode_cache(
                source_paths=[source],
                platforms=["Nintendo Switch"],
                cache_root=cache_root,
            )
            cached = read_barcode_catalog(cache_root / "nintendo-switch" / "catalog.csv")

        self.assertEqual(results["Nintendo Switch"], 1)
        self.assertEqual(results["all"], 1)
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0].barcode, "045496905651")

    def test_builds_cache_from_pricecharting_style_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "pricecharting.csv"
            cache_root = root / "barcodes"
            source.write_text(
                "id,product-name,console-name,upc,release-date\n"
                "123,Super Mario Galaxy + Super Mario Galaxy 2,Nintendo Switch,045496905651,2025-10-02\n"
                "456,Example Xbox Game,Xbox Series X|S,889842123456,2024-01-01\n",
                encoding="utf-8",
            )

            results = build_barcode_cache(
                source_paths=[source],
                cache_root=cache_root,
                provider="pricecharting",
            )
            switch_entries = read_barcode_catalog(cache_root / "nintendo-switch" / "catalog.csv")

        self.assertEqual(results["Nintendo Switch"], 1)
        self.assertEqual(results["Xbox Series X|S"], 1)
        self.assertEqual(results["all"], 2)
        self.assertEqual(switch_entries[0].provider, "pricecharting")
        self.assertEqual(switch_entries[0].provider_game_id, "123")

    @unittest.skipUnless(ATTACHED_BARCODE_PHOTO.exists(), "attached barcode sample is unavailable")
    def test_detect_barcodes_reads_attached_sample(self) -> None:
        self.assertIn("045496905651", detect_barcodes(ATTACHED_BARCODE_PHOTO, platform="Nintendo Switch"))


if __name__ == "__main__":
    unittest.main()
