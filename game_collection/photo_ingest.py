from __future__ import annotations

from pathlib import Path

from .cover_match import CoverIndexEntry, match_cover, match_to_game_match
from .review import match_to_row
from .review import write_review


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".webp"}


class PhotoIngestError(RuntimeError):
    pass


def _load_image_dependencies():
    try:
        import cv2  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PhotoIngestError(
            "Photo cover matching requires optional image dependencies. Install with "
            "`python -m pip install -e '.[image]'`."
        ) from exc
    return cv2, Image


def detect_photo_candidates(
    *,
    photo_path: Path,
    crops_dir: Path,
    platform: str | None = None,
    cover_entries: list[CoverIndexEntry] | None = None,
    accept_threshold: float = 0.92,
    min_area_ratio: float = 0.015,
) -> list[dict[str, str]]:
    cv2, Image = _load_image_dependencies()
    if not photo_path.exists():
        raise PhotoIngestError(f"Photo does not exist: {photo_path}")

    crops_dir.mkdir(parents=True, exist_ok=True)
    image = cv2.imread(str(photo_path))
    if image is None:
        raise PhotoIngestError(f"Could not read image: {photo_path}")

    height, width = image.shape[:2]
    min_area = width * height * min_area_ratio
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h
        if area < min_area:
            continue
        aspect = max(w / max(h, 1), h / max(w, 1))
        if 1.1 <= aspect <= 3.8:
            boxes.append((x, y, w, h))

    boxes.sort(key=lambda box: (box[1], box[0]))
    rows: list[dict[str, str]] = []
    pil_source = Image.open(photo_path)
    for index, (x, y, w, h) in enumerate(boxes, start=1):
        crop_path = crops_dir / f"{photo_path.stem}-{index:03d}.jpg"
        crop = pil_source.crop((x, y, x + w, y + h))
        crop.save(crop_path)
        row = {
            "photo_path": str(photo_path),
            "crop_path": str(crop_path),
            "candidate_title": "",
            "platform": platform or "",
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
            "notes": "Detected cover rectangle; no cover index match was attempted.",
        }
        if cover_entries:
            cover_match = match_cover(crop_path, cover_entries)
            if cover_match:
                game_match = match_to_game_match(cover_match)
                row["candidate_title"] = game_match.title
                row = match_to_row(row, game_match, accept_threshold=accept_threshold)
                row["notes"] = (
                    f"cover_match_distance={cover_match.distance}; "
                    f"cover_path={cover_match.entry.cover_path}"
                )
        rows.append(row)
    return rows


def write_photo_candidates(
    *,
    photo_paths: list[Path],
    out_path: Path,
    crops_dir: Path,
    platform: str | None = None,
    cover_entries: list[CoverIndexEntry] | None = None,
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
