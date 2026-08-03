from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_collection import db


class CollectionStateTests(unittest.TestCase):
    def test_sold_game_keeps_completed_play_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.sqlite3"
            db.init_db(db_path)

            with db.connect(db_path) as conn:
                game_id = db.upsert_game(
                    conn,
                    provider="manual",
                    provider_game_id="gamecube::metroid prime",
                    title="Metroid Prime",
                    platform="Nintendo GameCube",
                )
                item_id = db.add_collection_item(conn, game_id=game_id)
                db.add_playthrough(conn, game_id=game_id, play_status="completed")
                db.mark_status(conn, collection_item_id=item_id, status="sold")

                row = next(iter(db.list_collection(conn)))

            self.assertEqual(row["acquisition_status"], "sold")
            self.assertEqual(row["latest_play_status"], "completed")

    def test_plan_next_excludes_completed_games(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.sqlite3"
            db.init_db(db_path)

            with db.connect(db_path) as conn:
                completed_id = db.upsert_game(
                    conn,
                    provider="manual",
                    provider_game_id="gamecube::metroid prime",
                    title="Metroid Prime",
                    platform="Nintendo GameCube",
                )
                db.add_collection_item(conn, game_id=completed_id)
                db.add_playthrough(conn, game_id=completed_id, play_status="completed")

                unplayed_id = db.upsert_game(
                    conn,
                    provider="manual",
                    provider_game_id="gamecube::paper mario ttyd",
                    title="Paper Mario: The Thousand-Year Door",
                    platform="Nintendo GameCube",
                )
                db.add_collection_item(conn, game_id=unplayed_id)
                db.add_playthrough(conn, game_id=unplayed_id, play_status="unplayed")

                planned = list(db.plan_next(conn))

            self.assertEqual([row["title"] for row in planned], ["Paper Mario: The Thousand-Year Door"])


if __name__ == "__main__":
    unittest.main()

