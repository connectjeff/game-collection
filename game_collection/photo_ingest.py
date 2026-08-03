from __future__ import annotations

import re
from pathlib import Path

from .review import write_review


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".webp"}


class PhotoIngestError(RuntimeError):
    pass


def _load_image_dependencies():
    try:
        import cv2  # type: ignore[import-not-found]
        import pytesseract  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PhotoIngestError(
            "Photo OCR requires optional image dependencies. Install with "
            "`python -m pip install -e '.[image]'` and install the Tesseract OCR binary."
        ) from exc
    return cv2, pytesseract, Image


def _clean_ocr_title(text: str) -> str:
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"[^A-Za-z0-9: '&!?.+\\-]", " ", raw_line)
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) >= 4 and any(ch.isalpha() for ch in line):
            lines.append(line)
    if not lines:
        return ""
    lines.sort(key=lambda item: (len(item.split()), len(item)), reverse=True)
    return lines[0][:120]


def _ocr_crop(pytesseract, image) -> str:
    candidates = []
    for angle in (0, 90, 180, 270):
        rotated = image.rotate(angle, expand=True)
        text = pytesseract.image_to_string(rotated, config="--psm 6")
        cleaned = _clean_ocr_title(text)
        if cleaned:
            candidates.append(cleaned)
    if not candidates:
        return ""
    candidates.sort(key=len, reverse=True)
    return candidates[0]


def detect_photo_candidates(
    *,
    photo_path: Path,
    crops_dir: Path,
    platform: str | None = None,
    min_area_ratio: float = 0.015,
) -> list[dict[str, str]]:
    cv2, pytesseract, Image = _load_image_dependencies()
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
        candidate_title = _ocr_crop(pytesseract, crop)
        rows.append(
            {
                "photo_path": str(photo_path),
                "crop_path": str(crop_path),
                "candidate_title": candidate_title,
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
                "notes": "OCR candidate generated automatically." if candidate_title else "No OCR title detected.",
            }
        )
    return rows


def write_photo_candidates(
    *,
    photo_paths: list[Path],
    out_path: Path,
    crops_dir: Path,
    platform: str | None = None,
) -> int:
    all_rows: list[dict[str, str]] = []
    for photo_path in photo_paths:
        all_rows.extend(detect_photo_candidates(photo_path=photo_path, crops_dir=crops_dir, platform=platform))
    write_review(out_path, all_rows)
    return len(all_rows)


def image_paths(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        raise PhotoIngestError(f"Path does not exist: {path}")
    return sorted(item for item in path.iterdir() if item.suffix.lower() in IMAGE_SUFFIXES)
