from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_collection.sample_validation import (
    SamplePhotoExpectation,
    validate_sample_photos,
    write_sample_expectations_template,
)


class FakeProvider:
    name = "igdb"


class SampleValidationTests(unittest.TestCase):
    def test_validate_sample_photos_reports_found_expected_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photo = root / "sample.jpg"
            photo.write_bytes(b"fake")
            report = root / "report.csv"

            def fake_detect(**kwargs):
                return [
                    {
                        "matched_title": "Halo Infinite",
                        "candidate_title": "Halo Infinite",
                        "confidence": "0.97",
                        "crop_path": str(root / "crop.jpg"),
                        "provider_game_id": "123",
                        "notes": "cover_match_distance=2",
                    }
                ]

            with (
                patch("game_collection.sample_validation.build_cover_index", return_value=[]),
                patch("game_collection.sample_validation.detect_photo_candidates", side_effect=fake_detect),
            ):
                result = validate_sample_photos(
                    expectations=[
                        SamplePhotoExpectation(
                            photo=photo,
                            platform="Xbox Series X|S",
                            expected_titles=["Halo Infinite"],
                        )
                    ],
                    provider=FakeProvider(),
                    report_path=report,
                    crops_dir=root / "crops",
                )

            self.assertEqual(result.found_count, 1)
            with report.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["found"], "yes")
            self.assertEqual(rows[0]["matched_title"], "Halo Infinite")

    def test_write_sample_expectations_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "expectations.json"

            write_sample_expectations_template(path)

            self.assertIn("photos/incoming/example.jpg", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
