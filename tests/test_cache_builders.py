from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_collection.cover_match import (
    PRIORITIZED_PLATFORMS,
    build_platform_cache,
    platform_cache_statuses,
    prebuild_prioritized_cover_indexes,
)


class FakeIgdbProvider:
    name = "igdb"

    def platforms(self, limit: int = 500):
        return [
            {"id": 49, "name": "Xbox One"},
            {"id": 167, "name": "PlayStation 5"},
            {"id": 169, "name": "Xbox Series X|S"},
        ]


class CacheBuilderTests(unittest.TestCase):
    def test_build_platform_cache_prioritizes_requested_systems(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "platforms.csv"

            platforms = build_platform_cache(provider=FakeIgdbProvider(), cache_path=cache_path)

        self.assertEqual(platforms[:4], PRIORITIZED_PLATFORMS)

    def test_prebuild_prioritized_cover_indexes_builds_each_priority_platform(self) -> None:
        def fake_build_cover_index(*, provider, platform, index_path, limit=1000, refresh=False):
            return [object(), object()]

        with patch("game_collection.cover_match.build_cover_index", side_effect=fake_build_cover_index) as build:
            results = prebuild_prioritized_cover_indexes(provider=FakeIgdbProvider(), limit=25)

        self.assertEqual(set(results), set(PRIORITIZED_PLATFORMS))
        self.assertTrue(all(count == 2 for count in results.values()))
        self.assertEqual(build.call_count, len(PRIORITIZED_PLATFORMS))

    def test_platform_cache_statuses_sort_cached_first(self) -> None:
        def fake_read_cover_index(path: Path):
            return [object()] if "playstation-5" in str(path) else []

        with patch("game_collection.cover_match.read_cover_index", side_effect=fake_read_cover_index):
            statuses = platform_cache_statuses("igdb", ["Xbox One", "PlayStation 5", "PlayStation 4"])

        self.assertEqual([status.name for status in statuses], ["PlayStation 5", "PlayStation 4", "Xbox One"])
        self.assertTrue(statuses[0].cached)


if __name__ == "__main__":
    unittest.main()
