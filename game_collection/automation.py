from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import db
from .providers import MetadataProvider
from .review import match_to_row, read_review, write_review


@dataclass(frozen=True)
class AutoIngestResult:
    imported: int
    skipped_existing: int
    needs_review: int
    audit_path: Path


def match_review_rows(
    *,
    provider: MetadataProvider,
    rows: list[dict[str, str]],
    accept_threshold: float,
    limit: int = 3,
) -> list[dict[str, str]]:
    matched_rows: list[dict[str, str]] = []
    for row in rows:
        candidate_title = row.get("candidate_title", "").strip()
        if not candidate_title:
            updated = dict(row)
            updated["decision"] = "review"
            matched_rows.append(updated)
            continue

        matches = provider.search(candidate_title, platform=row.get("platform") or None, limit=limit)
        best = matches[0] if matches else None
        updated = match_to_row(row, best, accept_threshold=accept_threshold)
        if best and len(matches) > 1:
            alternatives = [
                {
                    "provider_game_id": match.provider_game_id,
                    "title": match.title,
                    "platform": match.platform,
                    "confidence": match.confidence,
                }
                for match in matches[1:]
            ]
            updated["notes"] = json.dumps(
                {"best_raw": best.raw or {}, "alternatives": alternatives},
                ensure_ascii=True,
            )[:1000]
        matched_rows.append(updated)
    return matched_rows


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
            if skip_existing and db.has_collection_item(conn, game_id=game_id):
                skipped_existing += 1
                continue
            db.add_collection_item(conn, game_id=game_id, acquisition_status=status)
            db.add_playthrough(conn, game_id=game_id, play_status=played)
            imported += 1
        conn.commit()
    finally:
        conn.close()
    return imported, skipped_existing


def auto_import_review(
    *,
    db_path: Path,
    review_csv: Path,
    provider: MetadataProvider,
    audit_path: Path,
    accept_threshold: float,
    status: str,
    played: str,
    skip_existing: bool = True,
) -> AutoIngestResult:
    rows = read_review(review_csv)
    matched_rows = match_review_rows(provider=provider, rows=rows, accept_threshold=accept_threshold)
    write_review(audit_path, matched_rows)
    imported, skipped_existing = import_accepted_rows(
        db_path=db_path,
        rows=matched_rows,
        status=status,
        played=played,
        skip_existing=skip_existing,
    )
    needs_review = sum(1 for row in matched_rows if row.get("decision") != "accept")
    return AutoIngestResult(
        imported=imported,
        skipped_existing=skipped_existing,
        needs_review=needs_review,
        audit_path=audit_path,
    )
