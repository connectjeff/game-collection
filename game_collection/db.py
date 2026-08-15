from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


DEFAULT_DB_PATH = Path("collection.sqlite3")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_game_id TEXT NOT NULL,
    title TEXT NOT NULL,
    platform TEXT,
    release_date TEXT,
    developer TEXT,
    publisher TEXT,
    description TEXT,
    cover_url TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider, provider_game_id)
);

CREATE TABLE IF NOT EXISTS collection_items (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id),
    acquisition_status TEXT NOT NULL DEFAULT 'owned'
        CHECK (acquisition_status IN ('owned', 'would_sell', 'sold', 'loaned', 'wishlist')),
    condition_notes TEXT,
    acquired_on TEXT,
    sold_on TEXT,
    sold_price_cents INTEGER,
    sale_notes TEXT,
    location TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS playthroughs (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id),
    play_status TEXT NOT NULL
        CHECK (play_status IN ('unplayed', 'playing', 'completed', 'retired')),
    started_on TEXT,
    completed_on TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS game_tags (
    game_id INTEGER NOT NULL REFERENCES games(id),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (game_id, tag_id)
);

DROP VIEW IF EXISTS collection_summary;

CREATE VIEW collection_summary AS
SELECT
    ci.id AS collection_item_id,
    g.id AS game_id,
    g.title,
    g.platform,
    g.provider,
    g.provider_game_id,
    g.release_date,
    g.developer,
    g.publisher,
    g.description,
    ci.acquisition_status,
    ci.created_at AS collection_created_at,
    ci.updated_at AS collection_updated_at,
    MAX(p.created_at) AS latest_play_record_at,
    COALESCE(
        (
            SELECT p2.play_status
            FROM playthroughs p2
            WHERE p2.game_id = g.id
            ORDER BY p2.created_at DESC, p2.id DESC
            LIMIT 1
        ),
        'unplayed'
    ) AS latest_play_status,
    g.cover_url
FROM games g
JOIN collection_items ci ON ci.game_id = g.id
LEFT JOIN playthroughs p ON p.game_id = g.id
GROUP BY g.id, ci.id;
"""


def connect(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path = DEFAULT_DB_PATH) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def upsert_game(
    conn: sqlite3.Connection,
    *,
    provider: str,
    provider_game_id: str,
    title: str,
    platform: str | None = None,
    release_date: str | None = None,
    developer: str | None = None,
    publisher: str | None = None,
    description: str | None = None,
    cover_url: str | None = None,
    metadata_json: str = "{}",
) -> int:
    conn.execute(
        """
        INSERT INTO games (
            provider, provider_game_id, title, platform, release_date, developer,
            publisher, description, cover_url, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider, provider_game_id) DO UPDATE SET
            title = excluded.title,
            platform = excluded.platform,
            release_date = excluded.release_date,
            developer = excluded.developer,
            publisher = excluded.publisher,
            description = excluded.description,
            cover_url = excluded.cover_url,
            metadata_json = excluded.metadata_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            provider,
            provider_game_id,
            title,
            platform,
            release_date,
            developer,
            publisher,
            description,
            cover_url,
            metadata_json,
        ),
    )
    row = conn.execute(
        "SELECT id FROM games WHERE provider = ? AND provider_game_id = ?",
        (provider, provider_game_id),
    ).fetchone()
    return int(row["id"])


def add_collection_item(
    conn: sqlite3.Connection,
    *,
    game_id: int,
    acquisition_status: str = "owned",
    condition_notes: str | None = None,
    location: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO collection_items (game_id, acquisition_status, condition_notes, location)
        VALUES (?, ?, ?, ?)
        """,
        (game_id, acquisition_status, condition_notes, location),
    )
    return int(cursor.lastrowid)


def has_collection_item(conn: sqlite3.Connection, *, game_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM collection_items WHERE game_id = ? LIMIT 1",
        (game_id,),
    ).fetchone()
    return row is not None


def add_playthrough(
    conn: sqlite3.Connection,
    *,
    game_id: int,
    play_status: str,
    notes: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO playthroughs (game_id, play_status, notes) VALUES (?, ?, ?)",
        (game_id, play_status, notes),
    )
    return int(cursor.lastrowid)


def mark_status(conn: sqlite3.Connection, *, collection_item_id: int, status: str) -> None:
    sold_fields = ", sold_on = CASE WHEN ? = 'sold' THEN DATE('now') ELSE sold_on END"
    conn.execute(
        f"""
        UPDATE collection_items
        SET acquisition_status = ?, updated_at = CURRENT_TIMESTAMP {sold_fields}
        WHERE id = ?
        """,
        (status, status, collection_item_id),
    )


def list_collection(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            collection_item_id, game_id, title, platform, acquisition_status,
            latest_play_status, provider, provider_game_id, release_date,
            developer, publisher, description, cover_url, collection_created_at,
            collection_updated_at
        FROM collection_summary
        ORDER BY title COLLATE NOCASE, platform COLLATE NOCASE
        """
    )


def plan_next(conn: sqlite3.Connection, *, limit: int = 20) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT collection_item_id, game_id, title, platform, acquisition_status, latest_play_status, cover_url
        FROM collection_summary
        WHERE acquisition_status IN ('owned', 'would_sell')
          AND latest_play_status IN ('unplayed', 'playing')
        ORDER BY
            CASE latest_play_status WHEN 'playing' THEN 0 ELSE 1 END,
            title COLLATE NOCASE,
            platform COLLATE NOCASE
        LIMIT ?
        """,
        (limit,),
    )


def get_game_detail(conn: sqlite3.Connection, *, game_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            g.*,
            ci.id AS collection_item_id,
            ci.acquisition_status,
            ci.condition_notes,
            ci.acquired_on,
            ci.sold_on,
            ci.sold_price_cents,
            ci.sale_notes,
            ci.location,
            COALESCE(
                (
                    SELECT p2.play_status
                    FROM playthroughs p2
                    WHERE p2.game_id = g.id
                    ORDER BY p2.created_at DESC, p2.id DESC
                    LIMIT 1
                ),
                'unplayed'
            ) AS latest_play_status
        FROM games g
        JOIN collection_items ci ON ci.game_id = g.id
        WHERE g.id = ?
        ORDER BY ci.id DESC
        LIMIT 1
        """,
        (game_id,),
    ).fetchone()


def update_game_metadata(
    conn: sqlite3.Connection,
    *,
    game_id: int,
    title: str,
    platform: str | None,
    release_date: str | None,
    developer: str | None,
    publisher: str | None,
    description: str | None,
    cover_url: str | None,
) -> None:
    conn.execute(
        """
        UPDATE games
        SET
            title = ?,
            platform = ?,
            release_date = ?,
            developer = ?,
            publisher = ?,
            description = ?,
            cover_url = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (title, platform, release_date, developer, publisher, description, cover_url, game_id),
    )


def update_collection_item(
    conn: sqlite3.Connection,
    *,
    collection_item_id: int,
    acquisition_status: str,
    condition_notes: str | None,
    location: str | None,
    sale_notes: str | None,
) -> None:
    sold_on_expr = "CASE WHEN ? = 'sold' THEN COALESCE(sold_on, DATE('now')) ELSE NULL END"
    conn.execute(
        f"""
        UPDATE collection_items
        SET
            acquisition_status = ?,
            condition_notes = ?,
            location = ?,
            sale_notes = ?,
            sold_on = {sold_on_expr},
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            acquisition_status,
            condition_notes,
            location,
            sale_notes,
            acquisition_status,
            collection_item_id,
        ),
    )
