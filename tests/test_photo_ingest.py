from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_collection.barcode_match import BarcodeCatalogEntry
from game_collection.cover_cache import CoverIndexEntry
from game_collection.photo_ingest import detect_photo_candidates


class PhotoIngestBarcodeTests(unittest.TestCase):
    def test_detect_photo_candidates_uses_only_barcode_catalog_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "back.jpg"
            photo.write_bytes(b"fake")

            with patch("game_collection.photo_ingest.detect_barcodes", return_value=["045496905651"]):
                rows = detect_photo_candidates(
                    photo_path=photo,
                    crops_dir=Path(tmp) / "crops",
                    platform="Nintendo Switch",
                    barcode_entries=[
                        BarcodeCatalogEntry(
                            barcode="045496905651",
                            title="Super Mario Galaxy + Super Mario Galaxy 2",
                            platform="Nintendo Switch",
                        )
                    ],
                    cover_entries=[
                        CoverIndexEntry(
                            provider="igdb",
                            provider_game_id="12345",
                            title="Super Mario Galaxy + Super Mario Galaxy 2",
                            platform="Nintendo Switch",
                            release_date=None,
                            developer=None,
                            publisher=None,
                            description=None,
                            cover_url=None,
                            cover_path=Path(tmp) / "cover.jpg",
                        )
                    ],
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["matched_title"], "Super Mario Galaxy + Super Mario Galaxy 2")
        self.assertEqual(rows[0]["provider"], "igdb")
        self.assertEqual(rows[0]["provider_game_id"], "12345")
        self.assertEqual(rows[0]["confidence"], "1.00")
        self.assertIn("exact barcode catalog match", rows[0]["notes"])
        self.assertIn("cover_path=", rows[0]["notes"])

    def test_detect_photo_candidates_keeps_unmatched_barcode_for_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "back.jpg"
            photo.write_bytes(b"fake")

            with patch("game_collection.photo_ingest.detect_barcodes", return_value=["999999999999"]):
                rows = detect_photo_candidates(
                    photo_path=photo,
                    crops_dir=Path(tmp) / "crops",
                    platform="Nintendo Switch",
                    barcode_entries=[],
                    cover_entries=[],
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["matched_title"], "")
        self.assertEqual(rows[0]["provider_game_id"], "")
        self.assertIn("no barcode catalog match", rows[0]["notes"])

    def test_detect_photo_candidates_uses_live_lookup_after_local_miss(self) -> None:
        live_entry = BarcodeCatalogEntry(
            barcode="045496905651",
            title="Super Mario Galaxy + Super Mario Galaxy 2",
            platform="Nintendo Switch",
            provider="upcdev",
            provider_game_id="045496905651",
        )
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "back.jpg"
            photo.write_bytes(b"fake")

            with (
                patch("game_collection.photo_ingest.detect_barcodes", return_value=["045496905651"]),
                patch("game_collection.photo_ingest.lookup_live_barcode", return_value=live_entry) as lookup,
            ):
                rows = detect_photo_candidates(
                    photo_path=photo,
                    crops_dir=Path(tmp) / "crops",
                    platform="Nintendo Switch",
                    barcode_entries=[],
                    cover_entries=[],
                    live_lookup=True,
                )

        lookup.assert_called_once_with("045496905651", platform="Nintendo Switch")
        self.assertEqual(rows[0]["matched_title"], "Super Mario Galaxy + Super Mario Galaxy 2")
        self.assertEqual(rows[0]["source_provider"], "upcdev")
        self.assertIn("live barcode lookup: upcdev", rows[0]["notes"])


if __name__ == "__main__":
    unittest.main()
