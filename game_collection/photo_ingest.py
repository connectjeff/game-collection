from __future__ import annotations

from pathlib import Path

from .barcode_match import BarcodeCatalogEntry, detect_barcodes, match_barcode
from .barcode_sources import lookup_live_barcode
from .cover_cache import CoverIndexEntry
from .review import match_to_row
from .review import write_review


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".webp"}


class PhotoIngestError(RuntimeError):
    pass


def _load_image_dependencies():
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PhotoIngestError(
            "Barcode scanning requires image dependencies. Install with "
            "`python -m pip install -e .`."
        ) from exc
    return cv2


def detect_photo_candidates(
    *,
    photo_path: Path,
    crops_dir: Path,
    platform: str | None = None,
    cover_entries: list[CoverIndexEntry] | None = None,
    barcode_entries: list[BarcodeCatalogEntry] | None = None,
    live_lookup: bool = False,
    accept_threshold: float = 0.92,
    min_area_ratio: float | None = None,
) -> list[dict[str, str]]:
    _load_image_dependencies()
    if not photo_path.exists():
        raise PhotoIngestError(f"Photo does not exist: {photo_path}")

    crops_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    seen_barcodes: set[str] = set()
    for barcode in detect_barcodes(photo_path, platform=platform):
        if barcode in seen_barcodes:
            continue
        seen_barcodes.add(barcode)
        barcode_match = match_barcode(
            barcode,
            barcode_entries or [],
            platform=platform,
            cover_entries=cover_entries,
        )
        source_note = "exact barcode catalog match"
        if not barcode_match and live_lookup:
            live_entry = lookup_live_barcode(barcode, platform=platform)
            if live_entry:
                barcode_match = match_barcode(
                    barcode,
                    [live_entry],
                    platform=platform,
                    cover_entries=cover_entries,
                )
                source_note = f"live barcode lookup: {live_entry.provider or 'unknown'}"
        row = _blank_candidate_row(
            photo_path=photo_path,
            platform=platform,
            notes=f"barcode={barcode}; no barcode catalog match",
        )
        row["barcode"] = barcode
        if barcode_match:
            row["candidate_title"] = barcode_match.title
            row = match_to_row(row, barcode_match, accept_threshold=accept_threshold)
            cover_path = (barcode_match.raw or {}).get("cover_index_path")
            row["notes"] = f"barcode={barcode}; {source_note}"
            if cover_path:
                row["notes"] += f"; cover_path={cover_path}"
        rows.append(row)
    return rows


def _blank_candidate_row(
    *,
    photo_path: Path,
    platform: str | None,
    notes: str,
) -> dict[str, str]:
    return {
        "upload_path": str(photo_path),
        "sample_image_path": str(photo_path),
        "candidate_title": "",
        "platform": platform or "",
        "play_status": "",
        "barcode": "",
        "source_provider": "",
        "source_id": "",
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
        "notes": notes,
    }


def write_photo_candidates(
    *,
    photo_paths: list[Path],
    out_path: Path,
    crops_dir: Path,
    platform: str | None = None,
    cover_entries: list[CoverIndexEntry] | None = None,
    barcode_entries: list[BarcodeCatalogEntry] | None = None,
    live_lookup: bool = False,
    accept_threshold: float = 0.92,
) -> int:
    all_rows: list[dict[str, str]] = []
    for photo_path in photo_paths:
        all_rows.extend(
            detect_photo_candidates(
                photo_path=photo_path,
                crops_dir=crops_dir,
                platform=platform,
                cover_entries=cover_entries,
                barcode_entries=barcode_entries,
                live_lookup=live_lookup,
                accept_threshold=accept_threshold,
            )
        )
    write_review(out_path, all_rows)
    return len(all_rows)


def image_paths(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        raise PhotoIngestError(f"Path does not exist: {path}")
    return sorted(item for item in path.iterdir() if item.suffix.lower() in IMAGE_SUFFIXES)
