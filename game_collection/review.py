from __future__ import annotations

import csv
import json
from pathlib import Path

from .providers import GameMatch


INTAKE_FIELDS = [
    "upload_path",
    "sample_image_path",
    "candidate_title",
    "platform",
    "acquisition_status",
    "play_status",
    "barcode",
    "source_provider",
    "source_id",
    "provider",
    "provider_game_id",
    "matched_title",
    "release_date",
    "developer",
    "publisher",
    "description",
    "cover_url",
    "confidence",
    "decision",
    "notes",
]

LEGACY_FIELD_ALIASES = {
    "upload_path": "photo_path",
    "sample_image_path": "crop_path",
}


def read_review(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for field, legacy_field in LEGACY_FIELD_ALIASES.items():
            if not row.get(field) and row.get(legacy_field):
                row[field] = row[legacy_field]
    return rows


def write_review(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=INTAKE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in INTAKE_FIELDS})


def match_to_row(row: dict[str, str], match: GameMatch | None, *, accept_threshold: float = 0.85) -> dict[str, str]:
    updated = dict(row)
    if match is None:
        updated["decision"] = "review"
        return updated
    current_decision = row.get("decision")
    decision = current_decision if current_decision in {"accept", "ignore"} else "review"
    updated.update(
        {
            "provider": match.provider,
            "provider_game_id": match.provider_game_id,
            "matched_title": match.title,
            "release_date": match.release_date or "",
            "developer": match.developer or "",
            "publisher": match.publisher or "",
            "description": match.description or "",
            "cover_url": match.cover_url or "",
            "confidence": f"{match.confidence:.2f}",
            "decision": decision,
            "notes": json.dumps(match.raw or {}, ensure_ascii=True)[:1000],
        }
    )
    raw = match.raw or {}
    updated["barcode"] = str(raw.get("barcode") or row.get("barcode") or "")
    updated["source_provider"] = str(raw.get("source_provider") or row.get("source_provider") or "")
    updated["source_id"] = str(raw.get("source_id") or row.get("source_id") or "")
    if not updated.get("platform") and match.platform:
        updated["platform"] = match.platform
    return updated
