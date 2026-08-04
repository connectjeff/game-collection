from __future__ import annotations

import unittest

from game_collection.photo_ingest import DEFAULT_SHAPE_HINT, _cover_shape_hint


class PhotoIngestShapeHintTests(unittest.TestCase):
    def test_prioritized_platforms_use_blu_ray_case_shape(self) -> None:
        for platform in ["PlayStation 5", "PlayStation 4", "Xbox One", "Xbox Series X|S"]:
            hint = _cover_shape_hint(platform)

            self.assertAlmostEqual(hint.target_aspect, 1.27)
            self.assertLess(hint.max_aspect, DEFAULT_SHAPE_HINT.max_aspect)
            self.assertLess(hint.max_area_ratio, DEFAULT_SHAPE_HINT.max_area_ratio)

    def test_unknown_platform_uses_broad_shape_hint(self) -> None:
        self.assertEqual(_cover_shape_hint("Nintendo GameCube"), DEFAULT_SHAPE_HINT)

    def test_explicit_min_area_override_is_preserved(self) -> None:
        hint = _cover_shape_hint("PlayStation 5", min_area_ratio=0.05)

        self.assertEqual(hint.min_area_ratio, 0.05)
        self.assertAlmostEqual(hint.target_aspect, 1.27)


if __name__ == "__main__":
    unittest.main()
