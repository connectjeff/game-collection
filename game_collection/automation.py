from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import db


@dataclass(frozen=True)
class DuplicateAcceptedRow:
    title: str
    platform: str | None
    acquisition_status: str
    play_status: str


def import_accepted_rows(
    *,
    db_path: Path,
    rows: list[dict[str, str]],
    status: str,
    played: str,
    skip_existing: bool,
) -> tuple[int, int]:
    db.init_db(db_path)
    imported = 0
    skipped_existing = 0
    conn = db.connect(db_path)
    try:
        for row in rows:
            if row.get("decision") != "accept":
                continue
            if not row.get("provider") or not row.get("provider_game_id"):
                continue
            game_id = db.upsert_game(
                conn,
                provider=row["provider"],
                provider_game_id=row["provider_game_id"],
                title=row.get("matched_title") or row["candidate_title"],
                platform=row.get("platform") or None,
                release_date=row.get("release_date") or None,
                developer=row.get("developer") or None,
                publisher=row.get("publisher") or None,
                description=row.get("description") or None,
                cover_url=row.get("cover_url") or None,
                metadata_json=json.dumps({"review_notes": row.get("notes")}, ensure_ascii=True),
            )
            title = row.get("matched_title") or row["candidate_title"]
            existing_title = db.find_collection_item_by_title(
                conn,
                title=title,
                platform=row.get("platform") or None,
            )
            if skip_existing and (db.has_collection_item(conn, game_id=game_id) or existing_title):
                skipped_existing += 1
                continue
            row_status = row.get("acquisition_status") if row.get("acquisition_status") in {"owned", "would_sell", "sold", "loaned", "wishlist"} else status
            db.add_collection_item(conn, game_id=game_id, acquisition_status=row_status)
            db.add_playthrough(conn, game_id=game_id, play_status=row.get("play_status") or played)
            imported += 1
        conn.commit()
    finally:
        conn.close()
    return imported, skipped_existing


def find_duplicate_accepted_rows(*, db_path: Path, rows: list[dict[str, str]]) -> list[DuplicateAcceptedRow]:
    db.init_db(db_path)
    duplicates: list[DuplicateAcceptedRow] = []
    conn = db.connect(db_path)
    try:
        for row in rows:
            if row.get("decision") != "accept":
                continue
            title = (row.get("matched_title") or row.get("candidate_title") or "").strip()
            if not title:
                continue
            existing = db.find_collection_item_by_title(
                conn,
                title=title,
                platform=row.get("platform") or None,
            )
            if existing:
                duplicates.append(
                    DuplicateAcceptedRow(
                        title=str(existing["title"]),
                        platform=existing["platform"],
                        acquisition_status=str(existing["acquisition_status"]),
                        play_status=str(existing["latest_play_status"]),
                    )
                )
    finally:
        conn.close()
    return duplicates
