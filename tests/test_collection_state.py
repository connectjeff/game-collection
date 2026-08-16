from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_collection import db


class CollectionStateTests(unittest.TestCase):
    def test_init_db_removes_legacy_free_text_state_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.sqlite3"
            with db.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE collection_items (
                        id INTEGER PRIMARY KEY,
                        game_id INTEGER NOT NULL,
                        acquisition_status TEXT NOT NULL DEFAULT 'owned',
                        condition_notes TEXT,
                        acquired_on TEXT,
                        sold_on TEXT,
                        sold_price_cents INTEGER,
                        sale_notes TEXT,
                        location TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE playthroughs (
                        id INTEGER PRIMARY KEY,
                        game_id INTEGER NOT NULL,
                        play_status TEXT NOT NULL,
                        started_on TEXT,
                        completed_on TEXT,
                        notes TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE tags (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE
                    );
                    CREATE TABLE game_tags (
                        game_id INTEGER NOT NULL,
                        tag_id INTEGER NOT NULL,
                        PRIMARY KEY (game_id, tag_id)
                    );
                    """
                )

            db.init_db(db_path)

            with db.connect(db_path) as conn:
                collection_columns = {row["name"] for row in conn.execute("PRAGMA table_info(collection_items)")}
                play_columns = {row["name"] for row in conn.execute("PRAGMA table_info(playthroughs)")}
                table_names = {
                    row["name"]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }

            self.assertNotIn("condition_notes", collection_columns)
            self.assertNotIn("sale_notes", collection_columns)
            self.assertNotIn("location", collection_columns)
            self.assertNotIn("acquired_on", collection_columns)
            self.assertNotIn("sold_on", collection_columns)
            self.assertNotIn("sold_price_cents", collection_columns)
            self.assertNotIn("notes", play_columns)
            self.assertNotIn("started_on", play_columns)
            self.assertNotIn("completed_on", play_columns)
            self.assertNotIn("tags", table_names)
            self.assertNotIn("game_tags", table_names)

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
                db.update_collection_item(conn, collection_item_id=item_id, acquisition_status="sold")

                row = next(iter(db.list_collection(conn)))

            self.assertEqual(row["acquisition_status"], "sold")
            self.assertEqual(row["latest_play_status"], "completed")

if __name__ == "__main__":
    unittest.main()
