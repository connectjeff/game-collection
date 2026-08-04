from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .cover_match import CoverIndexEntry, match_cover, match_to_game_match
from .review import match_to_row
from .review import write_review


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".webp"}


class PhotoIngestError(RuntimeError):
    pass


@dataclass(frozen=True)
class CoverShapeHint:
    target_aspect: float
    min_aspect: float
    max_aspect: float
    min_area_ratio: float
    max_area_ratio: float
    min_extent: float


DEFAULT_SHAPE_HINT = CoverShapeHint(
    target_aspect=1.35,
    min_aspect=1.1,
    max_aspect=3.8,
    min_area_ratio=0.015,
    max_area_ratio=0.85,
    min_extent=0.35,
)

BLU_RAY_CASE_HINT = CoverShapeHint(
    target_aspect=1.27,
    min_aspect=1.12,
    max_aspect=1.55,
    min_area_ratio=0.01,
    max_area_ratio=0.35,
    min_extent=0.48,
)


def _cover_shape_hint(platform: str | None, min_area_ratio: float | None = None) -> CoverShapeHint:
    normalized = (platform or "").casefold()
    if any(token in normalized for token in ("playstation 5", "playstation 4", "xbox one", "xbox series")):
        hint = BLU_RAY_CASE_HINT
    else:
        hint = DEFAULT_SHAPE_HINT
    if min_area_ratio is None or min_area_ratio == hint.min_area_ratio:
        return hint
    return CoverShapeHint(
        target_aspect=hint.target_aspect,
        min_aspect=hint.min_aspect,
        max_aspect=hint.max_aspect,
        min_area_ratio=min_area_ratio,
        max_area_ratio=hint.max_area_ratio,
        min_extent=hint.min_extent,
    )


def _intersection_over_union(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
    left_x, left_y, left_w, left_h = left
    right_x, right_y, right_w, right_h = right
    x1 = max(left_x, right_x)
    y1 = max(left_y, right_y)
    x2 = min(left_x + left_w, right_x + right_w)
    y2 = min(left_y + left_h, right_y + right_h)
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    if intersection == 0:
        return 0.0
    left_area = left_w * left_h
    right_area = right_w * right_h
    return intersection / max(left_area + right_area - intersection, 1)


def _ranked_cover_boxes(cv2, edges, image_area: int, hint: CoverShapeHint) -> list[tuple[int, int, int, int]]:
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed_edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(closed_edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    ranked: list[tuple[float, tuple[int, int, int, int]]] = []
    min_area = image_area * hint.min_area_ratio
    max_area = image_area * hint.max_area_ratio
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        box_area = w * h
        if box_area < min_area or box_area > max_area:
            continue
        aspect = max(w / max(h, 1), h / max(w, 1))
        if not hint.min_aspect <= aspect <= hint.max_aspect:
            continue
        contour_area = cv2.contourArea(contour)
        extent = contour_area / max(box_area, 1)
        if extent < hint.min_extent:
            continue
        aspect_penalty = abs(aspect - hint.target_aspect)
        area_ratio = box_area / max(image_area, 1)
        score = aspect_penalty - (extent * 0.25) - (area_ratio * 0.1)
        ranked.append((score, (x, y, w, h)))

    kept: list[tuple[int, int, int, int]] = []
    for _, box in sorted(ranked, key=lambda item: item[0]):
        if all(_intersection_over_union(box, kept_box) < 0.65 for kept_box in kept):
            kept.append(box)
    return sorted(kept, key=lambda box: (box[1], box[0]))


def _load_image_dependencies():
    try:
        import cv2  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PhotoIngestError(
            "Photo cover matching requires image dependencies. Install with "
            "`python -m pip install -e .`."
        ) from exc
    return cv2, Image


def detect_photo_candidates(
    *,
    photo_path: Path,
    crops_dir: Path,
    platform: str | None = None,
    cover_entries: list[CoverIndexEntry] | None = None,
    accept_threshold: float = 0.92,
    min_area_ratio: float | None = None,
) -> list[dict[str, str]]:
    cv2, Image = _load_image_dependencies()
    if not photo_path.exists():
        raise PhotoIngestError(f"Photo does not exist: {photo_path}")

    crops_dir.mkdir(parents=True, exist_ok=True)
    image = cv2.imread(str(photo_path))
    if image is None:
        raise PhotoIngestError(f"Could not read image: {photo_path}")

    height, width = image.shape[:2]
    image_area = width * height
    shape_hint = _cover_shape_hint(platform, min_area_ratio)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    boxes = _ranked_cover_boxes(cv2, edges, image_area, shape_hint)

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
            "play_status": "",
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
