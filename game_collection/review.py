from __future__ import annotations

import csv
import json
from pathlib import Path

from .providers import GameMatch


INTAKE_FIELDS = [
    "photo_path",
    "crop_path",
    "candidate_title",
    "platform",
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


def write_intake_template(photo_path: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=INTAKE_FIELDS)
        writer.writeheader()
        writer.writerow(
            {
                "photo_path": str(photo_path),
                "crop_path": "",
                "candidate_title": "",
                "platform": "",
                "provider": "",
                "provider_game_id": "",
                "matched_title": "",
                "release_date": "",
                "developer": "",
                "publisher": "",
                "description": "",
                "cover_url": "",
                "confidence": "",
                "decision": "review",
                "notes": "Fill candidate_title/platform, then run match-review.",
            }
        )


def read_review(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


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
    if not updated.get("platform") and match.platform:
        updated["platform"] = match.platform
    return updated
