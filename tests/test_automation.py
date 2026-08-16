from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_collection import db
from game_collection.automation import find_duplicate_accepted_rows, import_accepted_rows


class WebImportTests(unittest.TestCase):
    def test_import_accepted_rows_uses_row_state_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.sqlite3"
            rows = [
                {
                    "candidate_title": "Metroid Prime",
                    "platform": "Nintendo GameCube",
                    "acquisition_status": "would_sell",
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
            self.assertEqual(collection_rows[0]["acquisition_status"], "would_sell")

    def test_find_duplicate_accepted_rows_matches_existing_title_and_platform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                game_id = db.upsert_game(
                    conn,
                    provider="fake",
                    provider_game_id="metroid-prime",
                    title="Metroid Prime",
                    platform="Nintendo GameCube",
                )
                db.add_collection_item(conn, game_id=game_id, acquisition_status="owned")
                db.add_playthrough(conn, game_id=game_id, play_status="completed")

            duplicates = find_duplicate_accepted_rows(
                db_path=db_path,
                rows=[
                    {
                        "matched_title": "Metroid Prime",
                        "platform": "Nintendo GameCube",
                        "decision": "accept",
                    }
                ],
            )

        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0].title, "Metroid Prime")
        self.assertEqual(duplicates[0].play_status, "completed")


if __name__ == "__main__":
    unittest.main()
