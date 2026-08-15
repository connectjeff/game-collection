from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_collection.cover_cache import CoverIndexEntry, read_cover_index, write_cover_index


class CoverCacheTests(unittest.TestCase):
    def test_cover_index_round_trips_metadata_for_display(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "index.csv"
            cover_path = Path(tmp) / "covers" / "123.jpg"

            write_cover_index(
                index_path,
                [
                    CoverIndexEntry(
                        provider="igdb",
                        provider_game_id="123",
                        title="Example Game",
                        platform="PlayStation 5",
                        release_date="2026-01-01",
                        developer="Example Developer",
                        publisher="Example Publisher",
                        description="Example description",
                        cover_url="https://example.test/cover.jpg",
                        cover_path=cover_path,
                    )
                ],
            )

            entries = read_cover_index(index_path)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Example Game")
        self.assertEqual(entries[0].cover_path, cover_path)


if __name__ == "__main__":
    unittest.main()
