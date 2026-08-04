from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from game_collection.cover_match import CoverIndexEntry, match_cover, phash_path


class CoverMatchTests(unittest.TestCase):
    def test_match_cover_selects_nearest_index_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            red_cover = root / "red.jpg"
            blue_cover = root / "blue.jpg"
            crop = root / "crop.jpg"
            Image.new("RGB", (300, 400), "red").save(red_cover)
            Image.new("RGB", (300, 400), "blue").save(blue_cover)
            Image.new("RGB", (300, 400), "red").save(crop)

            entries = [
                CoverIndexEntry(
                    provider="fake",
                    provider_game_id="red",
                    title="Red Game",
                    platform=None,
                    release_date=None,
                    developer=None,
                    publisher=None,
                    description=None,
                    cover_url=None,
                    cover_path=red_cover,
                    phash=phash_path(red_cover),
                ),
                CoverIndexEntry(
                    provider="fake",
                    provider_game_id="blue",
                    title="Blue Game",
                    platform=None,
                    release_date=None,
                    developer=None,
                    publisher=None,
                    description=None,
                    cover_url=None,
                    cover_path=blue_cover,
                    phash=phash_path(blue_cover),
                ),
            ]

            match = match_cover(crop, entries)

            self.assertIsNotNone(match)
            self.assertEqual(match.entry.provider_game_id, "red")
            self.assertEqual(match.distance, 0)
            self.assertEqual(match.confidence, 1.0)


if __name__ == "__main__":
    unittest.main()

