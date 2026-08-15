from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .barcode_match import read_platform_barcode_cache
from .cover_cache import default_index_path, read_cover_index
from .photo_ingest import detect_photo_candidates
from .providers import MetadataProvider


REPORT_FIELDS = [
    "photo",
    "platform",
    "expected_title",
    "found",
    "matched_title",
    "confidence",
    "sample_image_path",
    "provider_game_id",
    "notes",
]

SUGGESTION_FIELDS = [
    "photo",
    "platform",
    "sample_image_path",
    "barcode",
    "source_provider",
    "source_id",
    "matched_title",
    "confidence",
    "decision",
    "provider",
    "provider_game_id",
    "notes",
]


@dataclass(frozen=True)
class SamplePhotoExpectation:
    photo: Path
    platform: str
    expected_titles: list[str]


@dataclass(frozen=True)
class SampleValidationResult:
    photo_count: int
    expected_count: int
    detected_count: int
    found_count: int
    report_path: Path
    suggestions_path: Path | None = None


def load_sample_expectations(path: Path) -> list[SamplePhotoExpectation]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    photos = payload.get("photos", payload if isinstance(payload, list) else [])
    expectations: list[SamplePhotoExpectation] = []
    for item in photos:
        if not isinstance(item, dict):
            continue
        expected_titles = [str(title) for title in item.get("expected_titles", []) if str(title).strip()]
        expectations.append(
            SamplePhotoExpectation(
                photo=Path(str(item["photo"])),
                platform=str(item["platform"]),
                expected_titles=expected_titles,
            )
        )
    return expectations


def write_sample_expectations_template(path: Path) -> None:
    template = {
        "photos": [
            {
                "photo": "photos/incoming/example.jpg",
                "platform": "PlayStation 5",
                "expected_titles": ["Example Game"],
            }
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(template, handle, indent=2)
        handle.write("\n")


def _normalize_title(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def _title_matches(expected: str, actual: str) -> bool:
    normalized_expected = _normalize_title(expected)
    normalized_actual = _normalize_title(actual)
    if not normalized_expected or not normalized_actual:
        return False
    return normalized_expected in normalized_actual or normalized_actual in normalized_expected


def _best_found_row(expected_title: str, rows: list[dict[str, str]]) -> dict[str, str] | None:
    for row in rows:
        matched_title = row.get("matched_title") or row.get("candidate_title", "")
        if _title_matches(expected_title, matched_title):
            return row
    return None


def validate_sample_photos(
    *,
    expectations: list[SamplePhotoExpectation],
    provider: MetadataProvider,
    report_path: Path,
    crops_dir: Path,
    suggestions_path: Path | None = None,
    cover_index_limit: int | None = None,
    refresh_cover_index: bool = False,
    accept_threshold: float = 0.92,
) -> SampleValidationResult:
    rows_by_photo: dict[Path, list[dict[str, str]]] = {}
    report_rows: list[dict[str, Any]] = []
    suggestion_rows: list[dict[str, Any]] = []
    found_count = 0

    for expectation in expectations:
        cover_entries = read_cover_index(default_index_path(provider.name, expectation.platform))
        barcode_entries = read_platform_barcode_cache(expectation.platform)
        rows = detect_photo_candidates(
            photo_path=expectation.photo,
            crops_dir=crops_dir / expectation.photo.stem,
            platform=expectation.platform,
            cover_entries=cover_entries,
            barcode_entries=barcode_entries,
            accept_threshold=accept_threshold,
        )
        rows_by_photo[expectation.photo] = rows
        for row in rows:
            suggestion_rows.append(
                {
                    "photo": str(expectation.photo),
                    "platform": expectation.platform,
                    "sample_image_path": row.get("sample_image_path", ""),
                    "barcode": row.get("barcode", ""),
                    "source_provider": row.get("source_provider", ""),
                    "source_id": row.get("source_id", ""),
                    "matched_title": row.get("matched_title", ""),
                    "confidence": row.get("confidence", ""),
                    "decision": row.get("decision", ""),
                    "provider": row.get("provider", ""),
                    "provider_game_id": row.get("provider_game_id", ""),
                    "notes": row.get("notes", ""),
                }
            )

        for expected_title in expectation.expected_titles:
            found_row = _best_found_row(expected_title, rows)
            if found_row:
                found_count += 1
            report_rows.append(
                {
                    "photo": str(expectation.photo),
                    "platform": expectation.platform,
                    "expected_title": expected_title,
                    "found": "yes" if found_row else "no",
                    "matched_title": (found_row or {}).get("matched_title", ""),
                    "confidence": (found_row or {}).get("confidence", ""),
                    "sample_image_path": (found_row or {}).get("sample_image_path", ""),
                    "provider_game_id": (found_row or {}).get("provider_game_id", ""),
                    "notes": (found_row or {}).get("notes", ""),
                }
            )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(report_rows)

    if suggestions_path:
        suggestions_path.parent.mkdir(parents=True, exist_ok=True)
        with suggestions_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=SUGGESTION_FIELDS)
            writer.writeheader()
            writer.writerows(suggestion_rows)

    return SampleValidationResult(
        photo_count=len(expectations),
        expected_count=sum(len(expectation.expected_titles) for expectation in expectations),
        detected_count=sum(len(rows) for rows in rows_by_photo.values()),
        found_count=found_count,
        report_path=report_path,
        suggestions_path=suggestions_path,
    )
