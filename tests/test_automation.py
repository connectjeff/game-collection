from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_collection import db
from game_collection.automation import auto_import_review, import_accepted_rows
from game_collection.providers import GameMatch


class FakeProvider:
    name = "fake"

    def search(self, title: str, platform: str | None = None, limit: int = 5) -> list[GameMatch]:
        confidence = 0.97 if title == "Metroid Prime" else 0.71
        return [
            GameMatch(
                provider="fake",
                provider_game_id=title.casefold().replace(" ", "-"),
                title=title,
                platform=platform,
                confidence=confidence,
                raw={"source": "fake"},
            )
        ]


class AutoIngestTests(unittest.TestCase):
    def test_auto_import_does_not_accept_by_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.csv"
            audit = root / "audit.csv"
            db_path = root / "collection.sqlite3"
            review.write_text(
                "\n".join(
                    [
                        "photo_path,crop_path,candidate_title,platform,provider,provider_game_id,matched_title,confidence,decision,notes",
                        "photo.jpg,,Metroid Prime,Nintendo GameCube,,,,,,",
                        "photo.jpg,,Prime,Nintendo GameCube,,,,,,",
                    ]
                ),
                encoding="utf-8",
            )

            result = auto_import_review(
                db_path=db_path,
                review_csv=review,
                provider=FakeProvider(),
                audit_path=audit,
                accept_threshold=0.92,
                status="owned",
                played="unplayed",
            )

            self.assertEqual(result.imported, 0)
            self.assertEqual(result.needs_review, 2)
            self.assertTrue(audit.exists())
            with db.connect(db_path) as conn:
                rows = list(db.list_collection(conn))
            self.assertEqual(rows, [])

    def test_auto_import_preserves_manual_accepts_and_skips_existing_games_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.csv"
            audit = root / "audit.csv"
            db_path = root / "collection.sqlite3"
            review.write_text(
                "\n".join(
                    [
                        "photo_path,crop_path,candidate_title,platform,provider,provider_game_id,matched_title,confidence,decision,notes",
                        "photo.jpg,,Metroid Prime,Nintendo GameCube,,,,,accept,",
                    ]
                ),
                encoding="utf-8",
            )

            kwargs = {
                "db_path": db_path,
                "review_csv": review,
                "provider": FakeProvider(),
                "audit_path": audit,
                "accept_threshold": 0.92,
                "status": "owned",
                "played": "unplayed",
            }
            first = auto_import_review(**kwargs)
            second = auto_import_review(**kwargs)

            self.assertEqual(first.imported, 1)
            self.assertEqual(second.imported, 0)
            self.assertEqual(second.skipped_existing, 1)

    def test_import_accepted_rows_uses_row_play_status_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.sqlite3"
            rows = [
                {
                    "candidate_title": "Metroid Prime",
                    "platform": "Nintendo GameCube",
                    "play_status": "completed",
                    "provider": "fake",
                    "provider_game_id": "metroid-prime",
                    "matched_title": "Metroid Prime",
                    "decision": "accept",
                }
            ]

            imported, skipped = import_accepted_rows(
                db_path=db_path,
                rows=rows,
                status="owned",
                played="unplayed",
                skip_existing=True,
            )

            self.assertEqual(imported, 1)
            self.assertEqual(skipped, 0)
            with db.connect(db_path) as conn:
                collection_rows = list(db.list_collection(conn))
            self.assertEqual(collection_rows[0]["latest_play_status"], "completed")


if __name__ == "__main__":
    unittest.main()
