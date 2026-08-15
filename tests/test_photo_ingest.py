from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_collection.barcode_match import BarcodeCatalogEntry
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
                    cover_entries=[],
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["matched_title"], "Super Mario Galaxy + Super Mario Galaxy 2")
        self.assertEqual(rows[0]["provider"], "barcode")
        self.assertEqual(rows[0]["provider_game_id"], "045496905651")
        self.assertEqual(rows[0]["confidence"], "1.00")
        self.assertIn("exact barcode catalog match", rows[0]["notes"])

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


if __name__ == "__main__":
    unittest.main()
