from __future__ import annotations

import email.policy
import json
import mimetypes
import html
import re
import sqlite3
import struct
import threading
import uuid
import urllib.parse
import zlib
from email.parser import BytesParser
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import db
from .automation import find_duplicate_accepted_rows, import_accepted_rows
from .barcode_match import BARCODE_CACHE_ROOT, barcode_cache_statuses, build_barcode_cache, read_platform_barcode_cache
from .barcode_sources import (
    BarcodeSourceError,
    download_csv_url,
    download_open_products_facts_products,
    download_upcdev_products,
    download_upcdev_search,
    download_wikidata_video_game_barcodes,
)
from .cover_cache import (
    CoverIndexEntry,
    PRIORITIZED_PLATFORMS,
    build_cover_index,
    build_platform_cache,
    default_index_path,
    find_or_fetch_cover_entry_for_title,
    platform_cache_statuses,
    prebuild_prioritized_cover_indexes,
    read_cover_index,
)
from .photo_ingest import IMAGE_SUFFIXES, PhotoIngestError, detect_photo_candidates
from .providers import ProviderError, get_provider
from .review import INTAKE_FIELDS, read_review, write_review


OWNERSHIP_STATUSES = ["owned", "would_sell", "sold", "loaned", "wishlist"]
PLAY_STATUSES = ["unplayed", "playing", "completed", "retired"]
DEFAULT_PROVIDER = "igdb"
EXPECTED_TITLE_COUNTS = list(range(1, 31))
PLATFORM_PRESETS = PRIORITIZED_PLATFORMS
WEB_INGEST_ROOT = Path("review/web-ingests")
BARCODE_SOURCE_ROOT = Path("review/barcode-sources")
AUTOCOMPLETE_LIMIT = 25
APP_ICON_BG = (12, 107, 88, 255)
APP_ICON_FG = (248, 250, 252, 255)
LAST_PLATFORM_COOKIE = "game_collection_last_platform"


def _h(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _selected(left: str | None, right: str) -> str:
    return " selected" if left == right else ""


def _cookie_value(header: str | None, name: str) -> str:
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return urllib.parse.unquote(value)
    return ""


def _blank_review_row(*, platform: str | None, play_status: str | None, note: str) -> dict[str, str]:
    return {
        "upload_path": "",
        "sample_image_path": "",
        "candidate_title": "",
        "platform": platform or "",
        "acquisition_status": "owned",
        "play_status": play_status or "unplayed",
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
        "notes": note,
    }


def _fit_review_rows_to_expected_count(
    rows: list[dict[str, str]],
    *,
    expected_count: int,
    platform: str | None,
    play_status: str | None,
) -> list[dict[str, str]]:
    fitted = rows[:expected_count]
    while len(fitted) < expected_count:
        fitted.append(
            _blank_review_row(
                platform=platform,
                play_status=play_status,
                note="Expected title placeholder; add a matched title manually.",
            )
        )
    return fitted


def _cached_platform_options(platforms: list[str]) -> list[str]:
    return [status.name for status in platform_cache_statuses("igdb", platforms) if status.cached]


def _normalize_title(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def _title_tokens(value: str) -> list[str]:
    return [token.casefold() for token in re.findall(r"[a-zA-Z0-9]+", value)]


def _title_initials(value: str) -> str:
    return "".join(token[0] for token in _title_tokens(value) if token)


@lru_cache(maxsize=128)
def _read_cover_index_cached(index_path: str, mtime_ns: int) -> tuple[CoverIndexEntry, ...]:
    return tuple(read_cover_index(Path(index_path)))


def _cached_cover_entries(provider: str, platform: str) -> list[CoverIndexEntry]:
    index_path = default_index_path(provider, platform)
    if not index_path.exists():
        return []
    return list(_read_cover_index_cached(str(index_path), index_path.stat().st_mtime_ns))


def _title_match_score(query: str, title: str) -> tuple[int, str]:
    normalized_query = _normalize_title(query)
    normalized_title = _normalize_title(title)
    if not normalized_query or not normalized_title:
        return (0, title.casefold())

    query_tokens = _title_tokens(query)
    title_tokens = _title_tokens(title)
    title_initials = _title_initials(title)

    score = 0
    if normalized_title == normalized_query:
        score += 1000
    if normalized_title.startswith(normalized_query):
        score += 800
    if normalized_query in normalized_title:
        score += 500
    if query_tokens and all(any(token in title_token for title_token in title_tokens) for token in query_tokens):
        score += 400 + (25 * len(query_tokens))
    if query_tokens and all(any(title_token.startswith(token) for title_token in title_tokens) for token in query_tokens):
        score += 250
    if title_initials.startswith(normalized_query):
        score += 220
    if normalized_query in title_initials:
        score += 140

    return (score, title.casefold())


def _autocomplete_matches(*, provider: str, platform: str, query: str, limit: int = AUTOCOMPLETE_LIMIT) -> list[CoverIndexEntry]:
    scored: list[tuple[int, str, CoverIndexEntry]] = []
    seen_ids: set[str] = set()
    for entry in _cached_cover_entries(provider, platform):
        if entry.provider_game_id in seen_ids:
            continue
        seen_ids.add(entry.provider_game_id)
        score, sort_title = _title_match_score(query, entry.title)
        if score <= 0:
            continue
        scored.append((score, sort_title, entry))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [entry for _, _, entry in scored[:limit]]


def _cached_exact_cover_entry(*, provider: str, platform: str, title: str) -> CoverIndexEntry | None:
    title_key = _normalize_title(title)
    if not title_key or not platform:
        return None
    for entry in _cached_cover_entries(provider, platform):
        if _normalize_title(entry.title) == title_key:
            return entry
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _cover_thumb(
    path: str | Path | None,
    label: str,
    alt: str,
    *,
    row_index: int | None = None,
    role: str | None = None,
    opens_modal: bool = False,
) -> str:
    sample_attrs = ""
    image_attrs = ""
    if row_index is not None:
        sample_attrs += f' data-row="{row_index}"'
        image_attrs += f' data-row="{row_index}"'
    if role:
        sample_attrs += f' data-role="{_h(role)}"'
        image_role = role.removesuffix("-sample")
        image_attrs += f' data-role="{_h(image_role)}"'
    if not path:
        hidden = " hidden" if role == "matched-cover-sample" else ""
        image_hidden = " hidden" if role == "matched-cover-sample" else ""
        return (
            f'<div class="cover-sample"{sample_attrs}{hidden}>'
            f'<img class="crop-thumb"{image_attrs}{image_hidden} alt="{_h(alt)}">'
            f'<span class="cover-label">{_h(label)}</span>'
            "</div>"
        )
    src = urllib.parse.quote(str(path))
    image = f'<img class="crop-thumb" src="/media?path={src}" alt="{_h(alt)}"{image_attrs}>'
    if opens_modal:
        image = (
            f'<button class="thumb-button" type="button" data-modal-image="/media?path={src}" '
            f'aria-label="Open {_h(label)} image">{image}</button>'
        )
    return (
        f'<div class="cover-sample"{sample_attrs}>'
        f"{image}"
        f'<span class="cover-label">{_h(label)}</span>'
        "</div>"
    )


def _matched_cover_path(row: dict[str, str]) -> str:
    provider = row.get("provider")
    platform = row.get("platform")
    provider_game_id = row.get("provider_game_id")
    if provider and platform and provider_game_id:
        indexed_cover = default_index_path(provider, platform).parent / "covers" / f"{provider_game_id}.jpg"
        if indexed_cover.exists():
            return str(indexed_cover)
    notes = row.get("notes", "")
    marker = "cover_path="
    if marker in notes:
        return notes.split(marker, 1)[1].split(";", 1)[0].strip()
    try:
        raw = json.loads(notes)
    except (TypeError, ValueError):
        return ""
    return str(raw.get("cover_index_path") or "")


def _apply_cover_entry_to_row(row: dict[str, str], entry: CoverIndexEntry) -> None:
    row["provider"] = entry.provider
    row["provider_game_id"] = entry.provider_game_id
    row["matched_title"] = entry.title
    row["candidate_title"] = row.get("candidate_title") or entry.title
    row["platform"] = entry.platform or row.get("platform") or ""
    row["release_date"] = entry.release_date or ""
    row["developer"] = entry.developer or ""
    row["publisher"] = entry.publisher or ""
    row["description"] = entry.description or ""
    row["cover_url"] = entry.cover_url or ""
    row["notes"] = (row.get("notes") or "").rstrip("; ")
    cover_note = f"cover_path={entry.cover_path}"
    if cover_note not in row["notes"]:
        row["notes"] = f"{row['notes']}; {cover_note}" if row["notes"] else cover_note


def _row_value(row: Any, key: str, default: str = "") -> str:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = row.get(key, default) if isinstance(row, dict) else default
    return default if value is None else str(value)


def _release_year(row: Any) -> int | None:
    release_date = _row_value(row, "release_date")
    if len(release_date) >= 4 and release_date[:4].isdigit():
        return int(release_date[:4])
    return None


def _release_era(row: Any) -> str:
    year = _release_year(row)
    if year is None:
        return "Unknown era"
    return f"{(year // 10) * 10}s"


def _top_values(rows: list[Any], key: str, *, limit: int = 8) -> list[str]:
    counts: dict[str, int] = {}
    for row in rows:
        value = _row_value(row, key).strip()
        if value:
            counts[value] = counts.get(value, 0) + 1
    return [
        value
        for value, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))[:limit]
    ]


def _top_eras(rows: list[Any], *, limit: int = 8) -> list[str]:
    counts: dict[str, int] = {}
    for row in rows:
        era = _release_era(row)
        if era != "Unknown era":
            counts[era] = counts.get(era, 0) + 1
    return [era for era, _count in sorted(counts.items(), key=lambda item: item[0], reverse=True)[:limit]]


def _filter_url(**updates: str) -> str:
    params = {key: value for key, value in updates.items() if value}
    query = urllib.parse.urlencode(params)
    return f"/?{query}" if query else "/"


def _chip(label: str, *, active: bool = False, **params: str) -> str:
    return (
        f'<a class="filter-chip{" active" if active else ""}" '
        f'href="{_h(_filter_url(**params))}">{_h(label)}</a>'
    )


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


@lru_cache(maxsize=4)
def _app_icon_png(size: int = 180) -> bytes:
    def blend(a: tuple[int, int, int, int], b: tuple[int, int, int, int], amount: float) -> tuple[int, int, int, int]:
        return tuple(int(a[index] + (b[index] - a[index]) * amount) for index in range(4))  # type: ignore[return-value]

    def rounded_rect(x: int, y: int, left: float, top: float, right: float, bottom: float, radius: float) -> bool:
        if left + radius <= x <= right - radius and top <= y <= bottom:
            return True
        if left <= x <= right and top + radius <= y <= bottom - radius:
            return True
        for cx, cy in (
            (left + radius, top + radius),
            (right - radius, top + radius),
            (left + radius, bottom - radius),
            (right - radius, bottom - radius),
        ):
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius**2:
                return True
        return False

    def triangle(x: int, y: int, points: tuple[tuple[float, float], tuple[float, float], tuple[float, float]]) -> bool:
        (x1, y1), (x2, y2), (x3, y3) = points
        denominator = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / denominator
        b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / denominator
        c = 1 - a - b
        return a >= 0 and b >= 0 and c >= 0

    barcode_lines = (0.31, 0.34, 0.39, 0.43, 0.47, 0.52, 0.57, 0.61, 0.66, 0.70)
    pixels = bytearray()
    card_left = size * 0.18
    card_top = size * 0.12
    card_right = size * 0.82
    card_bottom = size * 0.88
    card_radius = size * 0.055
    shadow_left = card_left + size * 0.035
    shadow_top = card_top + size * 0.035
    shadow_right = card_right + size * 0.035
    shadow_bottom = card_bottom + size * 0.035
    play_points = (
        (size * 0.43, size * 0.43),
        (size * 0.43, size * 0.59),
        (size * 0.59, size * 0.51),
    )
    for y in range(size):
        pixels.append(0)
        for x in range(size):
            vertical = y / max(size - 1, 1)
            horizontal = x / max(size - 1, 1)
            color = blend((17, 24, 32, 255), APP_ICON_BG, min(0.62, vertical * 0.72 + horizontal * 0.12))
            if (x - size * 0.23) ** 2 + (y - size * 0.09) ** 2 < (size * 0.24) ** 2:
                color = blend(color, (55, 132, 170, 255), 0.22)
            if rounded_rect(x, y, shadow_left, shadow_top, shadow_right, shadow_bottom, card_radius):
                color = blend(color, (0, 0, 0, 255), 0.22)
            if rounded_rect(x, y, card_left, card_top, card_right, card_bottom, card_radius):
                relative_y = (y - card_top) / (card_bottom - card_top)
                color = blend((248, 250, 252, 255), (213, 229, 235, 255), max(0, min(1, relative_y)))
            if rounded_rect(x, y, card_left, card_top, card_right, card_bottom, card_radius) and y < size * 0.28:
                color = blend((14, 77, 124, 255), (20, 142, 121, 255), horizontal)
            if card_left < x < card_right and size * 0.31 < y < size * 0.37:
                color = blend(color, (205, 46, 68, 255), 0.88)
            if triangle(x, y, play_points):
                color = (17, 24, 32, 255)
            if size * 0.31 < y < size * 0.72:
                for index, line_x in enumerate(barcode_lines):
                    width = size * (0.012 if index % 3 else 0.018)
                    if abs(x - size * line_x) < width:
                        color = blend(color, (17, 24, 32, 255), 0.82)
            if size * 0.70 < y < size * 0.74 and card_left + size * 0.07 < x < card_right - size * 0.07:
                color = blend(color, (17, 24, 32, 255), 0.72)
            if rounded_rect(x, y, card_left, card_top, card_right, card_bottom, card_radius):
                near_edge = (
                    abs(x - card_left) < size * 0.012
                    or abs(x - card_right) < size * 0.012
                    or abs(y - card_top) < size * 0.012
                    or abs(y - card_bottom) < size * 0.012
                )
                if near_edge:
                    color = blend(color, (255, 255, 255, 255), 0.36)
            pixels.extend(color)
    compressed = zlib.compress(bytes(pixels), level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def _app_manifest() -> bytes:
    return json.dumps(
        {
            "name": "Game Collection",
            "short_name": "Games",
            "description": "Local physical video game library manager.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#111820",
            "theme_color": "#0c6b58",
            "orientation": "any",
            "icons": [
                {"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
        },
        ensure_ascii=True,
    ).encode("utf-8")


def _service_worker() -> bytes:
    return b"""const CACHE_NAME = "game-collection-shell-v1";
const SHELL_URLS = ["/", "/ingest", "/caches", "/app.webmanifest", "/apple-touch-icon.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
"""


def _simple_page(kicker: str, title: str, message: str) -> str:
    return f"""
<div class="app-page">
  <section class="page-hero">
    <div class="page-kicker">{_h(kicker)}</div>
    <h1 class="page-title">{_h(title)}</h1>
    <p class="page-subtitle">{_h(message)}</p>
  </section>
</div>"""


def _handler_type(handler: Any) -> Any:
    return handler if isinstance(handler, type) else type(handler)


def _layout(title: str, body: str) -> bytes:
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0c6b58">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="Games">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <link rel="manifest" href="/app.webmanifest">
  <link rel="apple-touch-icon" href="/apple-touch-icon.png">
  <link rel="icon" href="/icon-512.png">
  <title>{_h(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1e252c;
      --muted: #65707c;
      --line: #d9dee5;
      --accent: #0c6b58;
      --accent-weak: #d9f0ea;
      --warn: #9b4f00;
    }}
    * {{ box-sizing: border-box; }}
    html {{
      min-height: 100%;
      -webkit-text-size-adjust: 100%;
    }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at 18% 0%, rgba(12, 107, 88, 0.22), transparent 34%),
        linear-gradient(180deg, #111820 0%, #172029 42%, #f6f7f9 100%);
      color: var(--text);
      font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      min-height: 100%;
      padding-top: env(safe-area-inset-top);
      -webkit-tap-highlight-color: rgba(12, 107, 88, 0.18);
    }}
    header {{
      background: rgba(17, 24, 32, 0.92);
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
      padding: 14px max(24px, env(safe-area-inset-right)) 14px max(24px, env(safe-area-inset-left));
      display: flex;
      align-items: center;
      gap: 18px;
      position: sticky;
      top: 0;
      z-index: 2;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      backdrop-filter: blur(16px);
    }}
    header a {{
      color: #eff6f4;
      font-weight: 700;
      text-decoration: none;
      min-height: 40px;
      display: inline-flex;
      align-items: center;
      white-space: nowrap;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px max(24px, env(safe-area-inset-right)) calc(24px + env(safe-area-inset-bottom)) max(24px, env(safe-area-inset-left));
    }}
    h1 {{ font-size: 24px; margin: 0 0 18px; }}
    h2 {{ font-size: 18px; margin: 28px 0 12px; }}
    .app-page {{
      margin: -24px;
      min-height: calc(100vh - 53px);
      padding: 22px max(24px, calc((100vw - 1180px) / 2 + 24px)) calc(40px + env(safe-area-inset-bottom));
      color: #f8fafc;
    }}
    .page-hero {{
      margin: 0 0 18px;
      max-width: 820px;
    }}
    .page-kicker {{
      color: #9bd6c9;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0;
      text-transform: uppercase;
      margin-bottom: 6px;
    }}
    .page-title {{
      margin: 0;
      color: #f8fafc;
      font-size: 30px;
      line-height: 1.08;
    }}
    .page-subtitle {{
      max-width: 760px;
      margin: 8px 0 0;
      color: #c8d3dd;
    }}
    .page-stack {{
      display: grid;
      gap: 16px;
    }}
    form.filters {{
      display: grid;
      grid-template-columns: minmax(180px, 1fr) 170px 170px auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 18px;
    }}
    input, select, textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      background: #fff;
      color: var(--text);
      font: inherit;
      font-size: 16px;
    }}
    textarea {{ min-height: 130px; resize: vertical; }}
    label {{ display: grid; gap: 5px; color: var(--muted); font-size: 13px; }}
    button, .button {{
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: #fff;
      padding: 10px 14px;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
      white-space: nowrap;
      min-height: 48px;
      touch-action: manipulation;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
    }}
    .button.secondary, button.secondary {{
      background: #e7ebef;
      color: var(--text);
    }}
    .button.danger, button.danger {{
      background: #b42318;
      color: #fff;
    }}
    .button-icon {{
      font-size: 17px;
      line-height: 1;
    }}
    input[type="file"] {{
      min-height: 54px;
      padding: 7px;
    }}
    input[type="file"]::file-selector-button {{
      min-height: 40px;
      margin-right: 10px;
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: #fff;
      font: inherit;
      font-weight: 800;
      padding: 8px 12px;
    }}
    input[type="checkbox"] {{
      width: 24px;
      height: 24px;
      accent-color: var(--accent);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: rgba(255, 255, 255, 0.96);
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 18px 38px rgba(0, 0, 0, 0.16);
    }}
    th, td {{
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--line);
      vertical-align: middle;
    }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: 0; }}
    .title-link {{ color: var(--accent); font-weight: 700; text-decoration: none; }}
    .library-shell {{
      margin: -24px;
      min-height: calc(100vh - 53px);
      padding: 22px 0 calc(38px + env(safe-area-inset-bottom));
      background:
        radial-gradient(circle at 18% 0%, rgba(12, 107, 88, 0.24), transparent 34%),
        linear-gradient(180deg, #111820 0%, #172029 42%, #f6f7f9 100%);
      color: #f8fafc;
      overflow: hidden;
    }}
    .library-hero {{
      padding: 8px 24px 12px;
      max-width: 1180px;
      margin: 0 auto;
    }}
    .library-kicker {{
      color: #9bd6c9;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0;
      text-transform: uppercase;
      margin-bottom: 6px;
    }}
    .library-title {{
      margin: 0;
      font-size: 30px;
      line-height: 1.08;
    }}
    .library-subtitle {{
      max-width: 760px;
      margin: 8px 0 18px;
      color: #c8d3dd;
    }}
    .library-filters {{
      display: grid;
      gap: 12px;
      margin-top: 14px;
    }}
    .library-search {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      max-width: 680px;
    }}
    .library-search-actions {{
      display: flex;
      gap: 8px;
      align-items: stretch;
    }}
    .library-search input {{
      min-height: 44px;
      border-color: rgba(255, 255, 255, 0.18);
      background: rgba(255, 255, 255, 0.09);
      color: #fff;
    }}
    .library-search input::placeholder {{ color: #aebac5; }}
    .filter-row {{
      display: flex;
      gap: 8px;
      overflow-x: auto;
      padding: 2px 24px 8px 0;
      scroll-snap-type: x proximity;
      -webkit-overflow-scrolling: touch;
    }}
    .filter-chip {{
      flex: 0 0 auto;
      scroll-snap-align: start;
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      padding: 7px 12px;
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.08);
      color: #eff6f4;
      font-size: 13px;
      font-weight: 800;
      text-decoration: none;
      white-space: nowrap;
    }}
    .filter-chip.active {{
      background: #f8fafc;
      color: #152029;
      border-color: #f8fafc;
    }}
    .library-summary {{
      padding: 0 24px;
      max-width: 1180px;
      margin: 0 auto 8px;
      color: #d4dde5;
      font-weight: 700;
    }}
    .shelf {{
      margin: 18px 0 0;
    }}
    .shelf-header {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 0 24px;
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: baseline;
    }}
    .shelf h2 {{
      margin: 0;
      color: #f8fafc;
      font-size: 19px;
    }}
    .shelf-note {{
      color: #b8c5cf;
      font-size: 13px;
      font-weight: 700;
    }}
    .shelf-row {{
      display: grid;
      grid-auto-flow: column;
      grid-auto-columns: 154px;
      gap: 12px;
      overflow-x: auto;
      padding: 12px max(24px, calc((100vw - 1180px) / 2 + 24px)) 18px;
      scroll-snap-type: x mandatory;
      scroll-padding-left: 24px;
      -webkit-overflow-scrolling: touch;
    }}
    .game-card {{
      scroll-snap-align: start;
      color: #f8fafc;
      text-decoration: none;
      display: grid;
      gap: 8px;
      min-width: 0;
    }}
    .poster {{
      width: 100%;
      aspect-ratio: 3 / 4;
      border-radius: 7px;
      overflow: hidden;
      background: #26313b;
      box-shadow: 0 12px 26px rgba(0, 0, 0, 0.28);
      border: 1px solid rgba(255, 255, 255, 0.12);
      transition: transform 150ms ease, box-shadow 150ms ease;
    }}
    .game-card:hover .poster, .game-card:focus .poster {{
      transform: translateY(-3px);
      box-shadow: 0 18px 34px rgba(0, 0, 0, 0.34);
    }}
    .poster img {{
      width: 100%;
      height: 100%;
      display: block;
      object-fit: cover;
    }}
    .poster-fallback {{
      width: 100%;
      height: 100%;
      display: grid;
      place-items: center;
      padding: 12px;
      color: #c8d3dd;
      text-align: center;
      font-weight: 800;
      background: linear-gradient(145deg, #25313d, #3b4450);
    }}
    .game-card-title {{
      color: #f8fafc;
      font-size: 13px;
      font-weight: 800;
      line-height: 1.22;
      min-height: 32px;
      overflow-wrap: anywhere;
    }}
    .game-card-meta {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      color: #b9c5cf;
      font-size: 11px;
      font-weight: 700;
    }}
    .mini-pill {{
      display: inline-flex;
      max-width: 100%;
      padding: 2px 6px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.1);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .library-empty {{
      max-width: 1180px;
      margin: 18px auto;
      padding: 18px 24px;
      color: #d4dde5;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--accent-weak);
      color: #124c41;
      font-size: 12px;
      font-weight: 700;
    }}
    .badge.sold {{ background: #eee3d5; color: var(--warn); }}
    .detail {{
      display: grid;
      grid-template-columns: 220px minmax(0, 1fr);
      gap: 22px;
      align-items: start;
    }}
    .cover-stack {{
      display: grid;
      gap: 12px;
    }}
    .metadata-list {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin: 0;
    }}
    .metadata-list div {{
      min-width: 0;
    }}
    .metadata-list dt {{
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 4px;
    }}
    .metadata-list dd {{
      margin: 0;
      overflow-wrap: anywhere;
      font-weight: 650;
    }}
    .metadata-description {{
      grid-column: 1 / -1;
    }}
    .metadata-description dd {{
      font-weight: 400;
      white-space: pre-wrap;
    }}
    .cover {{
      width: 100%;
      aspect-ratio: 3 / 4;
      object-fit: cover;
      background: #e2e6ea;
      border: 1px solid var(--line);
      border-radius: 6px;
    }}
    .placeholder {{
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      padding: 16px;
    }}
    .panel {{
      background: rgba(255, 255, 255, 0.96);
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 8px;
      padding: 18px;
      color: var(--text);
      box-shadow: 0 18px 38px rgba(0, 0, 0, 0.16);
    }}
    .panel h2:first-child {{
      margin-top: 0;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .actions {{ display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }}
    .muted {{ color: var(--muted); }}
    .notice {{
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-left: 4px solid var(--accent);
      background: rgba(255, 255, 255, 0.96);
      color: var(--text);
      padding: 12px 14px;
      border-radius: 6px;
      margin-bottom: 16px;
      box-shadow: 0 18px 38px rgba(0, 0, 0, 0.14);
    }}
    .notice.error {{ border-left-color: #b3261e; }}
    .upload-panel {{
      max-width: 760px;
    }}
    .ingest-summary-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(220px, 320px);
      gap: 16px;
      align-items: start;
    }}
    .uploaded-photos {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(92px, 1fr));
      gap: 8px;
    }}
    .uploaded-photo-thumb {{
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #e2e6ea;
    }}
    .manual-review {{
      margin-top: 18px;
    }}
    .manual-review-form {{
      display: grid;
      grid-template-columns: minmax(180px, 1fr) 150px minmax(260px, 2fr) auto;
      gap: 12px;
      align-items: end;
    }}
    .manual-preview {{
      display: flex;
      gap: 10px;
      align-items: center;
      margin-top: 12px;
      color: var(--muted);
    }}
    .single-review {{
      display: grid;
      grid-template-columns: minmax(180px, 280px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }}
    .single-review-covers {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .single-review .crop-thumb {{
      width: 118px;
      height: 154px;
    }}
    .single-review-fields {{
      display: grid;
      gap: 12px;
    }}
    .review-state-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .single-review-actions {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 4px;
    }}
    .single-review-actions button, .single-review-actions .button {{
      justify-content: center;
      text-align: center;
      width: 100%;
    }}
    .single-review-actions .reject {{
      background: #ece3db;
      color: var(--warn);
    }}
    .review-table input, .review-table select {{
      min-width: 0;
    }}
    .review-table textarea {{
      min-width: 0;
      min-height: 64px;
      resize: vertical;
      line-height: 1.3;
    }}
    .title-field {{
      overflow-wrap: anywhere;
    }}
    .review-table {{
      table-layout: fixed;
    }}
    .review-table th:nth-child(1), .review-table td:nth-child(1) {{ width: 210px; }}
    .review-table th:nth-child(2), .review-table td:nth-child(2) {{ width: 18%; }}
    .review-table th:nth-child(3), .review-table td:nth-child(3) {{ width: 130px; }}
    .review-table th:nth-child(4), .review-table td:nth-child(4) {{ width: auto; }}
    .review-table th:nth-child(5), .review-table td:nth-child(5) {{ width: 120px; }}
    .review-table th:nth-child(6), .review-table td:nth-child(6) {{ width: 1px; padding-left: 0; padding-right: 0; }}
    .decision-actions {{
      display: flex;
      gap: 6px;
      justify-content: flex-end;
    }}
    .icon-button {{
      width: 44px;
      height: 44px;
      display: inline-grid;
      place-items: center;
      padding: 0;
      border-radius: 6px;
      font-size: 17px;
      line-height: 1;
    }}
    .icon-button.accept {{
      background: var(--accent);
      color: #fff;
    }}
    .icon-button.ignore {{
      background: #ece3db;
      color: var(--warn);
    }}
    .icon-button.review {{
      background: #e7ebef;
      color: var(--text);
    }}
    .outcome-section {{
      margin-top: 22px;
      color: #f8fafc;
    }}
    .outcome-section h2 {{
      color: #f8fafc;
    }}
    .empty-state {{
      background: rgba(255, 255, 255, 0.96);
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 8px;
      color: var(--muted);
      padding: 14px;
      box-shadow: 0 18px 38px rgba(0, 0, 0, 0.14);
    }}
    .empty-state[hidden] {{ display: none; }}
    .match-control {{
      position: relative;
    }}
    .match-suggestions {{
      position: absolute;
      z-index: 4;
      left: 0;
      right: 0;
      top: calc(100% + 4px);
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      box-shadow: 0 8px 22px rgba(20, 31, 42, 0.14);
      max-height: 260px;
      overflow-y: auto;
    }}
    .match-suggestions[hidden] {{ display: none; }}
    .match-option {{
      display: flex;
      gap: 8px;
      align-items: center;
      width: 100%;
      border: 0;
      border-radius: 0;
      background: #fff;
      color: var(--text);
      padding: 8px 10px;
      text-align: left;
      font-weight: 600;
      white-space: normal;
    }}
    .match-option:hover, .match-option:focus {{
      background: var(--accent-weak);
      color: var(--text);
      outline: 0;
    }}
    .match-option small {{
      color: var(--muted);
      font-weight: 500;
    }}
    .crop-thumb {{
      width: 86px;
      height: 112px;
      object-fit: cover;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: #e2e6ea;
    }}
    .thumb-button {{
      display: block;
      padding: 0;
      border: 0;
      border-radius: 5px;
      background: transparent;
      cursor: zoom-in;
    }}
    .image-modal {{
      position: fixed;
      inset: 0;
      z-index: 10;
      display: grid;
      place-items: center;
      padding: 24px;
      background: rgba(20, 31, 42, 0.74);
    }}
    .image-modal[hidden] {{ display: none; }}
    .image-modal img {{
      max-width: min(96vw, 1200px);
      max-height: 92vh;
      object-fit: contain;
      border-radius: 8px;
      box-shadow: 0 18px 60px rgba(0, 0, 0, 0.35);
      background: #fff;
    }}
    .cover-pair {{
      display: flex;
      gap: 10px;
      align-items: start;
      min-width: 190px;
    }}
    .cover-sample {{
      display: grid;
      gap: 4px;
      justify-items: center;
    }}
    .cover-label {{
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }}
    @media (max-width: 760px) {{
      body {{ padding-bottom: calc(64px + env(safe-area-inset-bottom)); }}
      header {{
        position: fixed;
        top: auto;
        bottom: 0;
        left: 0;
        right: 0;
        justify-content: space-between;
        gap: 6px;
        padding: 8px max(10px, env(safe-area-inset-right)) calc(8px + env(safe-area-inset-bottom)) max(10px, env(safe-area-inset-left));
        border-top: 1px solid var(--line);
        border-bottom: 0;
        box-shadow: 0 -8px 24px rgba(20, 31, 42, 0.12);
      }}
      header a {{
        flex: 1 1 0;
        justify-content: center;
        min-width: 0;
        min-height: 46px;
        border-radius: 8px;
        background: #f1f4f6;
        color: #26313b;
        font-size: 12px;
        text-align: center;
        padding: 4px 6px;
      }}
      header a:first-child {{ font-size: 0; }}
      header a:first-child::after {{
        content: "Library";
        font-size: 12px;
      }}
      .page-subtitle {{
        font-size: 13px;
        line-height: 1.35;
      }}
      main {{
        padding-left: max(14px, env(safe-area-inset-left));
        padding-right: max(14px, env(safe-area-inset-right));
        padding-bottom: 20px;
      }}
      .app-page {{
        margin-left: -14px;
        margin-right: -14px;
        margin-top: -24px;
        min-height: calc(100vh - 64px);
        padding-left: 16px;
        padding-right: 16px;
        padding-bottom: 20px;
      }}
      .page-title {{ font-size: 25px; }}
      .library-shell {{
        margin-left: -14px;
        margin-right: -14px;
        margin-top: -24px;
        min-height: calc(100vh - 64px);
      }}
      .library-hero {{ padding-left: 16px; padding-right: 16px; }}
      .library-title {{ font-size: 25px; }}
      .library-search {{ grid-template-columns: minmax(0, 1fr) auto; }}
      .library-search-actions {{ justify-content: end; }}
      .filter-row {{ padding-right: 16px; }}
      .library-summary {{ padding-left: 16px; padding-right: 16px; }}
      .shelf-header {{ padding-left: 16px; padding-right: 16px; }}
      .shelf-row {{
        grid-auto-columns: 132px;
        gap: 10px;
        padding-left: 16px;
        padding-right: 16px;
      }}
      form.filters, .detail, .grid, .metadata-list, .ingest-summary-grid, .manual-review-form, .single-review, .review-state-grid {{ grid-template-columns: 1fr; }}
      table, thead, tbody, tr, th, td {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-bottom: 1px solid var(--line); padding: 8px 0; }}
      td {{
        border: 0;
        padding: 7px 12px;
      }}
      td[data-label]::before {{
        content: attr(data-label);
        display: block;
        margin-bottom: 3px;
        color: var(--muted);
        font-size: 11px;
        font-weight: 800;
        text-transform: uppercase;
      }}
      .review-table tr {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 8px;
        margin-bottom: 12px;
      }}
      .review-table td {{
        padding: 8px 12px;
      }}
      .decision-actions {{ justify-content: flex-start; }}
      .cover-pair {{ min-width: 0; }}
      .actions button, .actions .button {{
        flex: 1 1 96px;
      }}
      .single-review-actions {{
        position: sticky;
        bottom: calc(70px + env(safe-area-inset-bottom));
        z-index: 3;
        padding: 8px;
        margin-left: -8px;
        margin-right: -8px;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.94);
        box-shadow: 0 -8px 24px rgba(20, 31, 42, 0.14);
      }}
    }}
  </style>
  <script>
    const matchSuggestionState = new Map();
    let manualMatchSuggestions = [];
    let selectedManualMatch = null;

    function escapeHtml(value) {{
      return String(value || "").replace(/[&<>"']/g, (char) => ({{
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }}[char]));
    }}

    function setField(row, field, value) {{
      const element = document.querySelector(`[name="row_${{row}}_${{field}}"]`);
      if (element) element.value = value || "";
    }}

    function setMatchedCover(row, src) {{
      const image = document.querySelector(`[data-row="${{row}}"][data-role="matched-cover"]`);
      const sample = document.querySelector(`[data-row="${{row}}"][data-role="matched-cover-sample"]`);
      if (!image || !sample) return;
      if (src) {{
        image.src = `/media?path=${{encodeURIComponent(src)}}`;
        image.hidden = false;
        sample.hidden = false;
      }} else {{
        image.removeAttribute("src");
        image.hidden = true;
        sample.hidden = true;
      }}
    }}

    function applyMatch(row, match) {{
      setField(row, "provider", match.provider);
      setField(row, "provider_game_id", match.provider_game_id);
      setField(row, "matched_title", match.title);
      setField(row, "platform", match.platform);
      setField(row, "release_date", match.release_date);
      setField(row, "developer", match.developer);
      setField(row, "publisher", match.publisher);
      setField(row, "description", match.description);
      setField(row, "cover_url", match.cover_url);
      setField(row, "confidence", match.confidence);
      setField(row, "barcode", match.barcode);
      setField(row, "source_provider", match.source_provider);
      setField(row, "source_id", match.source_id);
      setField(row, "notes", match.notes);
      setMatchedCover(row, match.cover_path);
    }}

    function playStatusOptions(selected) {{
      return {json.dumps(PLAY_STATUSES)}.map((status) =>
        `<option value="${{escapeHtml(status)}}"${{status === selected ? " selected" : ""}}>${{escapeHtml(status)}}</option>`
      ).join("");
    }}

    function platformOptions(selected) {{
      const options = Array.from(document.querySelector("[data-role='manual-platform']")?.options || []).map((option) => option.value);
      if (selected && !options.includes(selected)) options.unshift(selected);
      return options.map((platform) =>
        `<option value="${{escapeHtml(platform)}}"${{platform === selected ? " selected" : ""}}>${{escapeHtml(platform)}}</option>`
      ).join("");
    }}

    function hiddenReviewMetadata(row, data) {{
      const fields = ["provider", "provider_game_id", "candidate_title", "release_date", "developer", "publisher", "description", "cover_url", "confidence", "notes", "barcode", "source_provider", "source_id"];
      return fields.map((field) =>
        `<input type="hidden" name="row_${{row}}_${{field}}" value="${{escapeHtml(data[field] || "")}}">`
      ).join("");
    }}

    function reviewRowHtml(row, data) {{
      const coverPath = data.cover_path || "";
      const matchedHidden = coverPath ? "" : " hidden";
      const matchedSrc = coverPath ? ` src="/media?path=${{encodeURIComponent(coverPath)}}"` : "";
      return `
<tr data-row="${{row}}" data-decision="review">
  <td>
    <div class="cover-pair">
      <div class="cover-sample"><div class="cover placeholder crop-thumb">Manual</div><span class="cover-label">Uploaded</span></div>
      <div class="cover-sample" data-row="${{row}}" data-role="matched-cover-sample"${{matchedHidden}}>
        <img class="crop-thumb" data-row="${{row}}" data-role="matched-cover" alt="Matched cached cover art"${{matchedSrc}}${{matchedHidden}}>
        <span class="cover-label">Matched</span>
      </div>
    </div>
    <input type="hidden" name="row_${{row}}_upload_path" value="">
    <input type="hidden" name="row_${{row}}_sample_image_path" value="">
  </td>
  <td><select name="row_${{row}}_platform" data-row="${{row}}" data-role="row-platform-select">${{platformOptions(data.platform || "")}}</select></td>
  <td><select name="row_${{row}}_play_status">${{playStatusOptions(data.play_status || "unplayed")}}</select></td>
  <td>
    <div class="match-control">
      <textarea class="title-field" name="row_${{row}}_matched_title" rows="2" data-row="${{row}}" data-role="match-title-input" autocomplete="off">${{escapeHtml(data.matched_title || "")}}</textarea>
      <div class="match-suggestions" data-row="${{row}}" data-role="match-suggestions" hidden></div>
    </div>
  </td>
  <td><div class="decision-actions" data-row="${{row}}" data-role="decision-actions">${{actionButtonsFor("review", row)}}</div><input type="hidden" name="row_${{row}}_decision" value="review"></td>
  <td>${{hiddenReviewMetadata(row, data)}}</td>
</tr>`;
    }}

    function updateOutcomeCounts() {{
      ["review", "accept", "ignore"].forEach((decision) => {{
        const tableBody = document.querySelector(`[data-role="${{decision}}-rows"]`);
        const empty = document.querySelector(`[data-role="${{decision}}-empty"]`);
        const count = tableBody ? tableBody.querySelectorAll("tr").length : 0;
        if (empty) empty.hidden = count !== 0;
        document.querySelectorAll(`[data-role="${{decision}}-count"]`).forEach((target) => {{
          target.textContent = String(count);
        }});
      }});
    }}

    function actionButtonsFor(decision, row) {{
      if (decision === "review") {{
        return `
          <button class="icon-button accept" type="button" data-row="${{row}}" data-action-decision="accept" title="Accept">&#10003;</button>
          <button class="icon-button ignore" type="button" data-row="${{row}}" data-action-decision="ignore" title="Ignore">&#10005;</button>
        `;
      }}
      if (decision === "accept") {{
        return `
          <button class="icon-button review" type="button" data-row="${{row}}" data-action-decision="review" title="Move to review">&#8634;</button>
          <button class="icon-button ignore" type="button" data-row="${{row}}" data-action-decision="ignore" title="Ignore">&#10005;</button>
        `;
      }}
      return `
        <button class="icon-button review" type="button" data-row="${{row}}" data-action-decision="review" title="Move to review">&#8634;</button>
        <button class="icon-button accept" type="button" data-row="${{row}}" data-action-decision="accept" title="Accept">&#10003;</button>
      `;
    }}

    function updateDecisionActions(row, decision) {{
      const actionContainer = document.querySelector(`[data-row="${{row}}"][data-role="decision-actions"]`);
      if (actionContainer) actionContainer.innerHTML = actionButtonsFor(decision, row);
    }}

    function moveReviewRow(row, decision) {{
      const tableRow = document.querySelector(`tr[data-row="${{row}}"]`);
      const target = document.querySelector(`[data-role="${{decision}}-rows"]`);
      if (!tableRow || !target) return;
      setField(row, "decision", decision);
      tableRow.dataset.decision = decision;
      updateDecisionActions(row, decision);
      target.appendChild(tableRow);
      updateOutcomeCounts();
    }}

    async function loadMatches(input) {{
      const row = input.dataset.row;
      const platform = document.querySelector(`[name="row_${{row}}_platform"]`)?.value || "";
      const q = input.value.trim();
      const panel = document.querySelector(`[data-row="${{row}}"][data-role="match-suggestions"]`);
      if (!panel || q.length < 2 || !platform) {{
        if (panel) panel.hidden = true;
        return [];
      }}
      const response = await fetch(`/matches?platform=${{encodeURIComponent(platform)}}&q=${{encodeURIComponent(q)}}`);
      if (!response.ok) {{
        panel.hidden = true;
        return [];
      }}
      const matches = await response.json();
      matchSuggestionState.set(row, matches);
      panel.innerHTML = matches.map((match, index) => `
        <button class="match-option" type="button" data-row="${{row}}" data-index="${{index}}">
          <span>${{match.title}}</span>
          <small>${{match.release_date || ""}}</small>
        </button>
      `).join("");
      panel.hidden = matches.length === 0;
      panel.querySelectorAll(".match-option").forEach((button) => {{
        button.addEventListener("mousedown", (event) => {{
          event.preventDefault();
          const selected = matchSuggestionState.get(row)?.[Number(button.dataset.index)];
          if (selected) applyMatch(row, selected);
          panel.hidden = true;
        }});
      }});
      return matches;
    }}

    function installMatchInput(input) {{
      if (input.dataset.listenerInstalled === "true") return;
      input.dataset.listenerInstalled = "true";
      let timer;
      input.addEventListener("input", () => {{
        clearTimeout(timer);
        timer = setTimeout(() => loadMatches(input), 180);
      }});
      input.addEventListener("focus", () => loadMatches(input));
      input.addEventListener("blur", () => {{
        const row = input.dataset.row;
        const matches = matchSuggestionState.get(row) || [];
        const exact = matches.find((match) => match.title.toLowerCase() === input.value.trim().toLowerCase());
        if (exact) applyMatch(row, exact);
        setTimeout(() => {{
          const panel = document.querySelector(`[data-row="${{row}}"][data-role="match-suggestions"]`);
          if (panel) panel.hidden = true;
        }}, 120);
      }});
    }}

    function installMatchInputs() {{
      document.querySelectorAll("[data-role='match-title-input']").forEach((input) => {{
        installMatchInput(input);
      }});
    }}

    function installPlatformSelector(select) {{
      if (select.dataset.listenerInstalled === "true") return;
      select.dataset.listenerInstalled = "true";
      select.addEventListener("change", () => {{
        const row = select.dataset.row;
        setField(row, "provider_game_id", "");
        setField(row, "release_date", "");
        setField(row, "developer", "");
        setField(row, "publisher", "");
        setField(row, "description", "");
        setField(row, "cover_url", "");
        setField(row, "confidence", "");
        setField(row, "notes", "");
        setMatchedCover(row, "");
        const input = document.querySelector(`[data-row="${{row}}"][data-role="match-title-input"]`);
        if (input) loadMatches(input);
      }});
    }}

    function installPlatformSelectors() {{
      document.querySelectorAll("[data-role='row-platform-select']").forEach((select) => installPlatformSelector(select));
    }}

    function updateManualPreview(match) {{
      const preview = document.querySelector("[data-role='manual-preview']");
      const image = document.querySelector("[data-role='manual-cover']");
      const title = document.querySelector("[data-role='manual-selected-title']");
      if (!preview || !image || !title) return;
      if (match && match.cover_path) {{
        image.src = `/media?path=${{encodeURIComponent(match.cover_path)}}`;
        image.hidden = false;
        title.textContent = match.title;
        preview.hidden = false;
      }} else {{
        image.removeAttribute("src");
        image.hidden = true;
        title.textContent = "";
        preview.hidden = true;
      }}
    }}

    function applyManualMatch(match) {{
      selectedManualMatch = match;
      const input = document.querySelector("[data-role='manual-title']");
      if (input) input.value = match.title || "";
      updateManualPreview(match);
    }}

    async function loadManualMatches() {{
      const platform = document.querySelector("[data-role='manual-platform']")?.value || "";
      const input = document.querySelector("[data-role='manual-title']");
      const panel = document.querySelector("[data-role='manual-suggestions']");
      const q = input?.value.trim() || "";
      selectedManualMatch = selectedManualMatch && selectedManualMatch.title === q && selectedManualMatch.platform === platform
        ? selectedManualMatch
        : null;
      updateManualPreview(selectedManualMatch);
      if (!input || !panel || q.length < 2 || !platform) {{
        if (panel) panel.hidden = true;
        return [];
      }}
      const response = await fetch(`/matches?platform=${{encodeURIComponent(platform)}}&q=${{encodeURIComponent(q)}}`);
      if (!response.ok) {{
        panel.hidden = true;
        return [];
      }}
      manualMatchSuggestions = await response.json();
      panel.innerHTML = manualMatchSuggestions.map((match, index) => `
        <button class="match-option" type="button" data-manual-match-index="${{index}}">
          <span>${{escapeHtml(match.title)}}</span>
          <small>${{escapeHtml(match.release_date || "")}}</small>
        </button>
      `).join("");
      panel.hidden = manualMatchSuggestions.length === 0;
      panel.querySelectorAll("[data-manual-match-index]").forEach((button) => {{
        button.addEventListener("mousedown", (event) => {{
          event.preventDefault();
          const match = manualMatchSuggestions[Number(button.dataset.manualMatchIndex)];
          if (match) applyManualMatch(match);
          panel.hidden = true;
        }});
      }});
      return manualMatchSuggestions;
    }}

    function addManualReviewRow() {{
      const rowCount = document.querySelector("[name='row_count']");
      const platform = document.querySelector("[data-role='manual-platform']")?.value || "";
      const playStatus = document.querySelector("[data-role='manual-play-status']")?.value || "unplayed";
      const input = document.querySelector("[data-role='manual-title']");
      const title = input?.value.trim() || "";
      const exact = manualMatchSuggestions.find((match) =>
        match.title.toLowerCase() === title.toLowerCase() && match.platform === platform
      );
      const match = selectedManualMatch || exact;
      if (!rowCount || !platform || !title || !match) {{
        alert("Choose a platform and select a matched title from the autocomplete results.");
        return;
      }}
      const row = Number(rowCount.value || "0");
      const data = {{
        provider: match.provider,
        provider_game_id: match.provider_game_id,
        candidate_title: match.title,
        matched_title: match.title,
        platform,
        play_status: playStatus,
        release_date: match.release_date,
        developer: match.developer,
        publisher: match.publisher,
        description: match.description,
        cover_url: match.cover_url,
        confidence: match.confidence,
        notes: match.notes,
        barcode: "",
        source_provider: "manual",
        source_id: match.provider_game_id,
        cover_path: match.cover_path
      }};
      const target = document.querySelector("[data-role='review-rows']");
      if (!target) return;
      target.insertAdjacentHTML("beforeend", reviewRowHtml(row, data));
      rowCount.value = String(row + 1);
      const newTitle = document.querySelector(`[data-row="${{row}}"][data-role="match-title-input"]`);
      const newPlatform = document.querySelector(`[data-row="${{row}}"][data-role="row-platform-select"]`);
      if (newTitle) installMatchInput(newTitle);
      if (newPlatform) installPlatformSelector(newPlatform);
      updateOutcomeCounts();
      if (input) input.value = "";
      selectedManualMatch = null;
      manualMatchSuggestions = [];
      updateManualPreview(null);
      const panel = document.querySelector("[data-role='manual-suggestions']");
      if (panel) panel.hidden = true;
    }}

    function installManualReview() {{
      const input = document.querySelector("[data-role='manual-title']");
      const platform = document.querySelector("[data-role='manual-platform']");
      const button = document.querySelector("[data-role='manual-add']");
      if (!input || !platform || !button) return;
      let timer;
      input.addEventListener("input", () => {{
        clearTimeout(timer);
        timer = setTimeout(loadManualMatches, 180);
      }});
      input.addEventListener("focus", loadManualMatches);
      input.addEventListener("blur", () => {{
        const exact = manualMatchSuggestions.find((match) =>
          match.title.toLowerCase() === input.value.trim().toLowerCase() && match.platform === platform.value
        );
        if (exact) applyManualMatch(exact);
        setTimeout(() => {{
          const panel = document.querySelector("[data-role='manual-suggestions']");
          if (panel) panel.hidden = true;
        }}, 120);
      }});
      platform.addEventListener("change", () => {{
        selectedManualMatch = null;
        manualMatchSuggestions = [];
        updateManualPreview(null);
        loadManualMatches();
      }});
      button.addEventListener("click", addManualReviewRow);
    }}

    function installDecisionActions() {{
      document.addEventListener("click", (event) => {{
        const button = event.target.closest("[data-action-decision]");
        if (!button) return;
        moveReviewRow(button.dataset.row, button.dataset.actionDecision);
      }});
      updateOutcomeCounts();
    }}

    function installImageModal() {{
      let modal = document.querySelector("[data-role='image-modal']");
      if (!modal) {{
        modal = document.createElement("div");
        modal.className = "image-modal";
        modal.dataset.role = "image-modal";
        modal.hidden = true;
        modal.innerHTML = '<img alt="Expanded uploaded cover">';
        document.body.appendChild(modal);
      }}
      const image = modal.querySelector("img");
      document.querySelectorAll("[data-modal-image]").forEach((button) => {{
        button.addEventListener("click", () => {{
          image.src = button.dataset.modalImage;
          modal.hidden = false;
        }});
      }});
      modal.addEventListener("click", () => {{
        modal.hidden = true;
        image.removeAttribute("src");
      }});
    }}

    function installResponsiveTables() {{
      document.querySelectorAll("table").forEach((table) => {{
        const labels = Array.from(table.querySelectorAll("thead th")).map((header) => header.textContent.trim());
        table.querySelectorAll("tbody tr").forEach((row) => {{
          Array.from(row.children).forEach((cell, index) => {{
            if (labels[index]) cell.dataset.label = labels[index];
          }});
        }});
      }});
    }}

    function installPlatformHintMemory() {{
      const select = document.querySelector("[data-role='platform-hint']");
      if (!select) return;
      const key = "game_collection_last_platform";
      const params = new URLSearchParams(window.location.search);
      const queryPlatform = params.get("platform") || "";
      const storedPlatform = (() => {{
        try {{ return window.localStorage.getItem(key) || ""; }}
        catch (_) {{ return ""; }}
      }})();
      const candidate = queryPlatform || storedPlatform;
      if (candidate && Array.from(select.options).some((option) => option.value === candidate)) {{
        select.value = candidate;
      }}
      const save = () => {{
        if (!select.value) return;
        try {{ window.localStorage.setItem(key, select.value); }}
        catch (_) {{}}
      }};
      select.addEventListener("change", save);
      select.form?.addEventListener("submit", save);
    }}

    function installServiceWorker() {{
      if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
      navigator.serviceWorker.register("/service-worker.js").catch(() => {{}});
    }}

    document.addEventListener("DOMContentLoaded", () => {{
      installResponsiveTables();
      installMatchInputs();
      installPlatformSelectors();
      installManualReview();
      installDecisionActions();
      installImageModal();
      installPlatformHintMemory();
      installServiceWorker();
    }});
  </script>
</head>
<body>
  <header aria-label="Primary navigation">
    <a href="/" aria-label="Library">Library</a>
    <a href="/ingest" aria-label="Scan a barcode">Scan</a>
    <a href="/caches" aria-label="Cache settings">Cache</a>
  </header>
  <main>{body}</main>
</body>
</html>"""
    return page.encode("utf-8")


class CollectionHandler(BaseHTTPRequestHandler):
    db_path: Path
    platform_options: list[str] = PLATFORM_PRESETS

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_html(self, title: str, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = _layout(title, body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_bytes(
        self,
        payload: bytes,
        *,
        content_type: str,
        cache_control: str = "public, max-age=86400",
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, path: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", path)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _redirect_remembering_platform(self, path: str, platform: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", path)
        self._remember_platform(platform)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _last_platform(self) -> str:
        return _cookie_value(self.headers.get("Cookie"), LAST_PLATFORM_COOKIE)

    def _remember_platform(self, platform: str) -> None:
        self.send_header(
            "Set-Cookie",
            f"{LAST_PLATFORM_COOKIE}={urllib.parse.quote(platform)}; Path=/; SameSite=Lax; Max-Age=31536000",
        )

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        parsed = urllib.parse.parse_qs(data, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _multipart_form(self) -> tuple[dict[str, str], list[dict[str, Any]]]:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        message = BytesParser(policy=email.policy.default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8") + body
        )
        fields: dict[str, str] = {}
        files: list[dict[str, Any]] = []
        if not message.is_multipart():
            return fields, files
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if not name:
                continue
            if filename:
                files.append(
                    {
                        "name": name,
                        "filename": Path(filename).name,
                        "content_type": part.get_content_type(),
                        "data": payload,
                    }
                )
            else:
                fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return fields, files

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/app.webmanifest":
            self._send_bytes(_app_manifest(), content_type="application/manifest+json")
            return
        if parsed.path == "/service-worker.js":
            self._send_bytes(
                _service_worker(),
                content_type="text/javascript; charset=utf-8",
                cache_control="no-cache",
            )
            return
        if parsed.path in {"/apple-touch-icon.png", "/icon-512.png"}:
            size = 512 if parsed.path == "/icon-512.png" else 180
            self._send_bytes(_app_icon_png(size), content_type="image/png")
            return
        if parsed.path == "/":
            self._send_html("Game Collection", self._collection(parsed.query))
            return
        if parsed.path == "/ingest":
            params = urllib.parse.parse_qs(parsed.query)
            message = (params.get("message") or [""])[0]
            platform = (params.get("platform") or [""])[0]
            self._send_html("Upload Photos", self._ingest_form(message or None, selected_platform=platform or None))
            return
        if parsed.path == "/caches":
            self._send_html("Cache Settings", self._cache_settings())
            return
        if parsed.path == "/matches":
            self._send_matches(parsed.query)
            return
        if parsed.path.startswith("/ingest/"):
            run_id = parsed.path.removeprefix("/ingest/").strip("/")
            self._send_html("Ingest Results", self._ingest_results(run_id))
            return
        if parsed.path == "/media":
            self._send_media(parsed.query)
            return
        if parsed.path.startswith("/games/"):
            try:
                game_id = int(parsed.path.removeprefix("/games/").strip("/"))
            except ValueError:
                self._send_html("Not Found", _simple_page("Missing Route", "Not Found", "That game URL is not valid."), HTTPStatus.NOT_FOUND)
                return
            self._send_html("Game Detail", self._game_detail(game_id))
            return
        self._send_html("Not Found", _simple_page("Missing Route", "Not Found", "That page does not exist."), HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/ingest":
            self._handle_ingest_upload()
            return
        if parsed.path == "/caches":
            self._handle_cache_settings()
            return
        if parsed.path == "/barcode-sources":
            self._handle_barcode_source_download()
            return
        if parsed.path == "/library-art-refresh":
            self._handle_library_art_refresh()
            return
        if parsed.path.startswith("/ingest/") and parsed.path.endswith("/review"):
            run_id = parsed.path.split("/")[2]
            self._handle_ingest_review(run_id)
            return
        form = self._form()
        with db.connect(self.db_path) as conn:
            if parsed.path.startswith("/items/") and parsed.path.endswith("/collection"):
                item_id = int(parsed.path.split("/")[2])
                db.update_collection_item(
                    conn,
                    collection_item_id=item_id,
                    acquisition_status=form["acquisition_status"],
                )
                if form.get("play_status"):
                    db.add_playthrough(
                        conn,
                        game_id=int(form["game_id"]),
                        play_status=form["play_status"],
                    )
                self._redirect(f"/games/{form['game_id']}")
                return
            if parsed.path.startswith("/items/") and parsed.path.endswith("/delete"):
                item_id = int(parsed.path.split("/")[2])
                db.delete_collection_item(conn, collection_item_id=item_id)
                self._redirect("/")
                return
            if parsed.path.startswith("/games/") and parsed.path.endswith("/play"):
                game_id = int(parsed.path.split("/")[2])
                db.add_playthrough(
                    conn,
                    game_id=game_id,
                    play_status=form["play_status"],
                )
                self._redirect(f"/games/{game_id}")
                return
        self._send_html("Not Found", _simple_page("Missing Route", "Not Found", "That action does not exist."), HTTPStatus.NOT_FOUND)

    def _send_media(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        raw_path = (params.get("path") or [""])[0]
        try:
            allowed_roots = [WEB_INGEST_ROOT.resolve(), Path("review/cover-indexes").resolve()]
            media_path = Path(raw_path).resolve()
            if not any(_is_relative_to(media_path, root) for root in allowed_roots):
                raise ValueError
        except (ValueError, OSError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not media_path.exists() or not media_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
        payload = media_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_matches(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        q = (params.get("q") or [""])[0].strip()
        platform = (params.get("platform") or [""])[0].strip()
        if len(q) < 2 or not platform:
            self._send_json([])
            return
        matches = []
        for entry in _autocomplete_matches(provider="igdb", platform=platform, query=q):
            matches.append(
                {
                    "provider": entry.provider,
                    "provider_game_id": entry.provider_game_id,
                    "title": entry.title,
                    "platform": entry.platform or platform,
                    "release_date": entry.release_date or "",
                    "developer": entry.developer or "",
                    "publisher": entry.publisher or "",
                    "description": entry.description or "",
                    "cover_url": entry.cover_url or "",
                    "cover_path": str(entry.cover_path),
                    "confidence": "",
                    "source_provider": "manual",
                    "source_id": entry.provider_game_id,
                    "barcode": "",
                    "notes": json.dumps({"manual_match": True, "cover_index_path": str(entry.cover_path)}, ensure_ascii=True),
                }
            )
        self._send_json(matches)

    def _collection(self, query: str) -> str:
        params = urllib.parse.parse_qs(query)
        q_raw = (params.get("q") or [""])[0].strip()
        q = q_raw.casefold()
        owning = (params.get("owning") or [""])[0]
        played = (params.get("played") or [""])[0]
        platform_filter = (params.get("platform") or [""])[0]
        publisher_filter = (params.get("publisher") or [""])[0]
        era_filter = (params.get("era") or [""])[0]
        message = (params.get("message") or [""])[0]
        conn = db.connect(self.db_path)
        try:
            rows = [dict(row) for row in db.list_collection(conn)]
        finally:
            conn.close()
        filtered = []
        for row in rows:
            text = " ".join(
                [
                    _row_value(row, "title"),
                    _row_value(row, "platform"),
                    _row_value(row, "publisher"),
                    _row_value(row, "developer"),
                ]
            ).casefold()
            if q and q not in text:
                continue
            if owning and _row_value(row, "acquisition_status") != owning:
                continue
            if played and _row_value(row, "latest_play_status") != played:
                continue
            if platform_filter and _row_value(row, "platform") != platform_filter:
                continue
            if publisher_filter and _row_value(row, "publisher") != publisher_filter:
                continue
            if era_filter and _release_era(row) != era_filter:
                continue
            filtered.append(row)
        filtered.sort(key=lambda row: _row_value(row, "title").casefold())
        active_params = {
            "q": q_raw,
            "owning": owning,
            "played": played,
            "platform": platform_filter,
            "publisher": publisher_filter,
            "era": era_filter,
        }
        platform_chips = [
            _chip(platform, active=platform == platform_filter, **{**active_params, "platform": platform})
            for platform in _top_values(rows, "platform", limit=10)
        ]
        publisher_chips = [
            _chip(publisher, active=publisher == publisher_filter, **{**active_params, "publisher": publisher})
            for publisher in _top_values(rows, "publisher", limit=8)
        ]
        era_chips = [
            _chip(era, active=era == era_filter, **{**active_params, "era": era})
            for era in _top_eras(rows, limit=8)
        ]
        ownership_chips = [
            _chip("All", active=not owning, **{**active_params, "owning": ""}),
            *[
                _chip(status.replace("_", " ").title(), active=owning == status, **{**active_params, "owning": status})
                for status in OWNERSHIP_STATUSES
            ],
        ]
        play_chips = [
            _chip("Any Play Status", active=not played, **{**active_params, "played": ""}),
            *[
                _chip(status.title(), active=played == status, **{**active_params, "played": status})
                for status in PLAY_STATUSES
            ],
        ]
        clear_search = (
            '<a class="button secondary" href="/" aria-label="Clear search and filters">'
            '<span class="button-icon">&#8634;</span></a>'
            if any(active_params.values())
            else ""
        )
        body = f"""
<div class="library-shell">
  <section class="library-hero">
    <div class="library-kicker">Physical Library</div>
    <h1 class="library-title">Browse your games by what to play, keep, finish, or sell.</h1>
    <p class="library-subtitle">Swipe shelves by platform, publisher, release era, ownership, and play status.</p>
    <form class="library-search" method="get" action="/">
      <input name="q" value="{_h(q_raw)}" placeholder="Search title, platform, publisher, or developer">
      <div class="library-search-actions">
        <button type="submit" aria-label="Search"><span class="button-icon">&#128269;</span></button>
        {clear_search}
      </div>
      <input type="hidden" name="owning" value="{_h(owning)}">
      <input type="hidden" name="played" value="{_h(played)}">
      <input type="hidden" name="platform" value="{_h(platform_filter)}">
      <input type="hidden" name="publisher" value="{_h(publisher_filter)}">
      <input type="hidden" name="era" value="{_h(era_filter)}">
    </form>
    <div class="library-filters">
      <div class="filter-row">{''.join(ownership_chips)}</div>
      <div class="filter-row">{''.join(play_chips)}</div>
      <div class="filter-row">{_chip("All Platforms", active=not platform_filter, **{**active_params, "platform": ""})}{''.join(platform_chips)}</div>
      <div class="filter-row">{_chip("All Publishers", active=not publisher_filter, **{**active_params, "publisher": ""})}{''.join(publisher_chips)}</div>
      <div class="filter-row">{_chip("All Eras", active=not era_filter, **{**active_params, "era": ""})}{''.join(era_chips)}</div>
    </div>
  </section>
  {f'<div class="notice">{_h(message)}</div>' if message else ''}
  <p class="library-summary">{len(filtered)} shown from {len(rows)} collection item(s).</p>
  {_handler_type(self)._library_shelves(self, filtered)}
</div>
"""
        return body

    def _library_shelves(self, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return '<div class="library-empty">No games match this view.</div>'

        shelves: list[str] = []
        playing = [row for row in rows if _row_value(row, "latest_play_status") == "playing"]
        if playing:
            shelves.append(_handler_type(self)._game_shelf(self, "Continue Playing", playing, "Games already in progress."))

        up_next = [
            row
            for row in rows
            if _row_value(row, "acquisition_status") in {"owned", "would_sell"}
            and _row_value(row, "latest_play_status") in {"unplayed", "playing"}
        ]
        if up_next:
            shelves.append(_handler_type(self)._game_shelf(self, "Up Next", up_next[:24], "Owned games that are not completed."))

        recent = sorted(rows, key=lambda row: _row_value(row, "collection_created_at"), reverse=True)[:24]
        shelves.append(_handler_type(self)._game_shelf(self, "Recently Added", recent, "Newest physical copies in the library."))

        sell_candidates = [row for row in rows if _row_value(row, "acquisition_status") == "would_sell"]
        if sell_candidates:
            shelves.append(_handler_type(self)._game_shelf(self, "Considering Selling", sell_candidates, "Marked as possible sale candidates."))

        completed = [row for row in rows if _row_value(row, "latest_play_status") == "completed"]
        if completed:
            shelves.append(_handler_type(self)._game_shelf(self, "Completed Archive", completed[:24], "Finished games remain visible even after sale."))

        for platform in _top_values(rows, "platform", limit=5):
            platform_rows = [row for row in rows if _row_value(row, "platform") == platform]
            shelves.append(_handler_type(self)._game_shelf(self, platform, platform_rows[:24], "Grouped by platform metadata."))

        for publisher in _top_values(rows, "publisher", limit=4):
            publisher_rows = [row for row in rows if _row_value(row, "publisher") == publisher]
            if len(publisher_rows) >= 2:
                shelves.append(_handler_type(self)._game_shelf(self, f"{publisher} Shelf", publisher_rows[:24], "Publisher metadata from IGDB or imported sources."))

        for era in _top_eras(rows, limit=4):
            era_rows = [row for row in rows if _release_era(row) == era]
            if era_rows:
                shelves.append(_handler_type(self)._game_shelf(self, f"{era} Releases", era_rows[:24], "Grouped by release year."))

        return "".join(shelves)

    def _game_shelf(self, title: str, rows: list[dict[str, Any]], note: str) -> str:
        cards = "".join(_handler_type(self)._game_card(self, row) for row in rows)
        return f"""
<section class="shelf">
  <div class="shelf-header">
    <h2>{_h(title)}</h2>
    <span class="shelf-note">{_h(note)}</span>
  </div>
  <div class="shelf-row">{cards}</div>
</section>"""

    def _game_card(self, row: dict[str, Any]) -> str:
        cover_url = _row_value(row, "cover_url")
        title = _row_value(row, "title")
        platform = _row_value(row, "platform")
        publisher = _row_value(row, "publisher")
        status = _row_value(row, "latest_play_status")
        owning = _row_value(row, "acquisition_status")
        year = _release_year(row)
        poster = (
            f'<img src="{_h(cover_url)}" alt="Cover art for {_h(title)}" loading="lazy">'
            if cover_url
            else f'<div class="poster-fallback">{_h(title)}</div>'
        )
        meta_parts = [
            platform,
            str(year) if year else "",
            publisher,
            status.title() if status else "",
            owning.replace("_", " ").title() if owning else "",
        ]
        meta = "".join(f'<span class="mini-pill">{_h(part)}</span>' for part in meta_parts if part)
        return f"""
<a class="game-card" href="/games/{_h(_row_value(row, 'game_id'))}" aria-label="{_h(title)}">
  <div class="poster">{poster}</div>
  <div class="game-card-title">{_h(title)}</div>
  <div class="game-card-meta">{meta}</div>
</a>"""

    def _cache_settings(self, message: str | None = None, *, error: bool = False) -> str:
        notice = f'<div class="notice{" error" if error else ""}">{_h(message)}</div>' if message else ""
        statuses = platform_cache_statuses("igdb", self.platform_options)
        barcode_statuses = barcode_cache_statuses(self.platform_options)
        rows = []
        for status in statuses:
            checked = " checked" if status.cached else ""
            metadata_text = f"{status.count} metadata rows" if status.cached else "not cached"
            barcode_count = barcode_statuses.get(status.name, 0)
            barcode_text = f"{barcode_count} barcode rows" if barcode_count else "no barcode cache"
            rows.append(
                f"""
<tr>
  <td><input type="checkbox" name="platform" value="{_h(status.name)}"{checked}></td>
  <td>{_h(status.name)}</td>
  <td><span class="badge{' sold' if not status.cached else ''}">{_h(metadata_text)}</span></td>
  <td><span class="badge{' sold' if not barcode_count else ''}">{_h(barcode_text)}</span></td>
</tr>"""
            )
        return f"""
<div class="app-page">
  <section class="page-hero">
    <div class="page-kicker">Local Indexes</div>
    <h1 class="page-title">Cache Settings</h1>
    <p class="page-subtitle">Build the local metadata, cover art, and barcode caches used by upload review and manual entry.</p>
  </section>
  {notice}
  <div class="page-stack">
    <section class="panel">
      <p class="muted">Choose platforms to build local metadata indexes for title autocomplete and cover art display. Barcode caches are built from local CSV sources with the CLI. Cached platforms are listed first; uncached platforms are alphabetical.</p>
      <form method="post" action="/caches">
        <table>
          <thead><tr><th></th><th>Platform</th><th>Metadata Cache</th><th>Barcode Cache</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
        <div class="actions"><button type="submit"><span class="button-icon">&#9881;</span>Build</button></div>
      </form>
    </section>
    <section class="panel">
      <h2>Barcode Sources</h2>
      <p class="muted">Download public barcode source data into ignored local CSVs, then rebuild barcode caches from all downloaded source files.</p>
      <form method="post" action="/barcode-sources">
        <div class="grid">
          <label>Source
            <select name="source">
              <option value="wikidata-video-games">Wikidata Video Games</option>
              <option value="upcdev-search">upc.dev Search</option>
              <option value="upcdev-product">upc.dev Barcode Lookup</option>
              <option value="open-products-facts">Open Products Facts Barcode Lookup</option>
              <option value="csv-url">CSV URL</option>
            </select>
          </label>
          <label>Query
            <input name="query" placeholder="Required for upc.dev search">
          </label>
          <label>Barcodes
            <input name="barcodes" placeholder="Comma or newline separated">
          </label>
          <label>CSV URL
            <input name="url" placeholder="https://example.com/barcodes.csv">
          </label>
          <label>Limit
            <input name="limit" type="number" min="1" placeholder="Optional">
          </label>
          <label>Offset
            <input name="offset" type="number" min="0" placeholder="Optional">
          </label>
        </div>
        <label><input type="checkbox" name="incremental" value="1" checked> Merge with existing source CSV</label>
        <div class="actions"><button type="submit"><span class="button-icon">&#8635;</span>Fetch</button></div>
      </form>
    </section>
    <section class="panel">
      <h2>Library Art</h2>
      <p class="muted">Re-query IGDB for games already in your library that do not have cover art yet. Newer games often gain artwork after the first import.</p>
      <form method="post" action="/library-art-refresh">
        <div class="actions"><button type="submit"><span class="button-icon">&#8635;</span>Refresh</button></div>
      </form>
    </section>
  </div>
</div>
"""

    def _handle_cache_settings(self) -> None:
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
        selected = [item for item in form.get("platform", []) if item]
        if not selected:
            self._send_html("Cache Settings", self._cache_settings("Choose at least one platform.", error=True))
            return
        try:
            provider = get_provider("igdb")
            built: list[str] = []
            for platform in selected:
                entries = build_cover_index(
                    provider=provider,
                    platform=platform,
                    index_path=default_index_path("igdb", platform),
                    limit=None,
                    refresh=True,
                )
                built.append(f"{platform}: {len(entries)} covers")
            self._send_html("Cache Settings", self._cache_settings("Built indexes for " + "; ".join(built)))
        except (ProviderError, ValueError) as exc:
            self._send_html("Cache Settings", self._cache_settings(str(exc), error=True), HTTPStatus.BAD_REQUEST)

    def _handle_barcode_source_download(self) -> None:
        form = self._form()
        source = form.get("source") or ""
        source_path = BARCODE_SOURCE_ROOT / f"{source}.csv"
        incremental = form.get("incremental") == "1"
        limit = int(form["limit"]) if form.get("limit") else None
        offset = int(form["offset"]) if form.get("offset") else None
        barcodes = [
            item.strip()
            for item in re.split(r"[\s,]+", form.get("barcodes", ""))
            if item.strip()
        ]
        try:
            if source == "wikidata-video-games":
                entries = download_wikidata_video_game_barcodes(
                    source_path,
                    limit=limit,
                    offset=offset,
                    incremental=incremental,
                )
            elif source == "upcdev-search":
                query = form.get("query", "").strip()
                if not query:
                    raise BarcodeSourceError("Query is required for upc.dev search.")
                entries = download_upcdev_search(source_path, query=query, incremental=incremental)
            elif source == "upcdev-product":
                if not barcodes:
                    raise BarcodeSourceError("At least one barcode is required for upc.dev lookup.")
                entries = download_upcdev_products(source_path, barcodes=barcodes, incremental=incremental)
            elif source == "open-products-facts":
                if not barcodes:
                    raise BarcodeSourceError("At least one barcode is required for Open Products Facts lookup.")
                entries = download_open_products_facts_products(source_path, barcodes=barcodes, incremental=incremental)
            elif source == "csv-url":
                url = form.get("url", "").strip()
                if not url:
                    raise BarcodeSourceError("CSV URL is required.")
                entries = download_csv_url(source_path, url=url, incremental=incremental)
            else:
                raise BarcodeSourceError("Choose a supported barcode source.")
            results = build_barcode_cache(source_paths=[BARCODE_SOURCE_ROOT], cache_root=BARCODE_CACHE_ROOT)
            cache_summary = "; ".join(f"{platform}: {count}" for platform, count in results.items())
            self._send_html(
                "Cache Settings",
                self._cache_settings(f"Downloaded {len(entries)} source row(s) from {source}; rebuilt barcode caches: {cache_summary}"),
            )
        except (BarcodeSourceError, ValueError, OSError) as exc:
            self._send_html("Cache Settings", self._cache_settings(str(exc), error=True), HTTPStatus.BAD_REQUEST)

    def _handle_library_art_refresh(self) -> None:
        try:
            provider = get_provider(DEFAULT_PROVIDER)
            updated = 0
            checked = 0
            skipped = 0
            seen_game_ids: set[int] = set()
            with db.connect(self.db_path) as conn:
                rows = [dict(row) for row in db.list_collection(conn)]
                for row in rows:
                    game_id = int(row["game_id"])
                    if game_id in seen_game_ids:
                        continue
                    seen_game_ids.add(game_id)
                    if row.get("cover_url"):
                        skipped += 1
                        continue
                    title = row.get("title") or ""
                    platform = row.get("platform") or None
                    checked += 1
                    entry = find_or_fetch_cover_entry_for_title(
                        provider=provider,
                        platform=platform,
                        title=title,
                        index_path=default_index_path(DEFAULT_PROVIDER, platform),
                    )
                    if not entry:
                        continue
                    db.update_game_metadata(
                        conn,
                        game_id=game_id,
                        title=entry.title,
                        platform=entry.platform or platform,
                        release_date=entry.release_date or row.get("release_date"),
                        developer=entry.developer or row.get("developer"),
                        publisher=entry.publisher or row.get("publisher"),
                        description=entry.description or row.get("description"),
                        cover_url=entry.cover_url or row.get("cover_url"),
                    )
                    updated += 1
            self._send_html(
                "Cache Settings",
                self._cache_settings(
                    f"Refreshed library art: checked {checked}, updated {updated}, skipped {skipped} with existing art."
                ),
            )
        except (ProviderError, ValueError) as exc:
            self._send_html("Cache Settings", self._cache_settings(str(exc), error=True), HTTPStatus.BAD_REQUEST)

    def _ingest_form(
        self,
        message: str | None = None,
        *,
        error: bool = False,
        selected_platform: str | None = None,
    ) -> str:
        notice = f'<div class="notice{" error" if error else ""}">{_h(message)}</div>' if message else ""
        cached_platforms = _cached_platform_options(self.platform_options)
        current_platform = selected_platform or (self._last_platform() if not isinstance(self, type) else "")
        if current_platform not in cached_platforms:
            current_platform = ""
        platform_options = "".join(
            f'<option value="{_h(platform)}"{_selected(current_platform, platform)}>{_h(platform)}</option>'
            for platform in cached_platforms
        )
        platform_control = (
            f'<select name="platform" required data-role="platform-hint">{platform_options}</select>'
            if cached_platforms
            else '<select name="platform" required disabled data-role="platform-hint"><option value="">No cached platforms</option></select>'
        )
        return f"""
<div class="app-page">
  <section class="page-hero">
    <div class="page-kicker">Barcode Ingest</div>
    <h1 class="page-title">Scan Game</h1>
    <p class="page-subtitle">Upload or take one back-cover photo. The app scans for one barcode match, then gives you a fast visual confirmation screen.</p>
  </section>
  {notice}
  <section class="panel upload-panel">
    <form method="post" action="/ingest" enctype="multipart/form-data">
      <label>Game case barcode photo
        <input type="file" name="photos" accept="image/*" required>
      </label>
      <div class="grid">
        <label>Platform hint
          {platform_control}
        </label>
      </div>
      <div class="actions"><button type="submit"><span class="button-icon">&#128247;</span>Scan</button></div>
    </form>
  </section>
</div>
"""

    def _run_dir(self, run_id: str) -> Path:
        return WEB_INGEST_ROOT / run_id

    def _audit_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "audit.csv"

    def _handle_ingest_upload(self) -> None:
        try:
            db.init_db(self.db_path)
            fields, files = self._multipart_form()
            image_files = [
                item
                for item in files
                if item["name"] in {"photos", "photo_library", "camera_photo"} and item["data"]
            ]
            if not image_files:
                self._send_html("Upload Photos", self._ingest_form("Choose at least one photo.", error=True))
                return

            run_id = uuid.uuid4().hex
            run_dir = self._run_dir(run_id)
            uploads_dir = run_dir / "uploads"
            crops_dir = run_dir / "crops"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            crops_dir.mkdir(parents=True, exist_ok=True)

            provider_name = fields.get("provider") or DEFAULT_PROVIDER
            provider = get_provider(provider_name)
            platform = fields.get("platform") or None
            status = fields.get("status") or "owned"
            played = fields.get("played") or "unplayed"
            if not platform:
                self._send_html("Upload Photos", self._ingest_form("Choose a cached platform.", error=True))
                return
            cover_index_path = default_index_path(provider_name, platform)
            cover_entries = read_cover_index(cover_index_path)
            barcode_entries = read_platform_barcode_cache(platform)
            rows: list[dict[str, str]] = []
            for index, file_info in enumerate(image_files, start=1):
                suffix = Path(file_info["filename"]).suffix.lower() or ".jpg"
                upload_path = uploads_dir / f"upload-{index:03d}{suffix}"
                upload_path.write_bytes(file_info["data"])
                rows.extend(
                    detect_photo_candidates(
                        photo_path=upload_path,
                        crops_dir=crops_dir,
                        platform=platform,
                        cover_entries=cover_entries,
                        barcode_entries=barcode_entries,
                        live_lookup=True,
                        accept_threshold=2.0,
                    )
                )
            for row in rows:
                if row.get("matched_title") and not _matched_cover_path(row):
                    try:
                        cover_entry = find_or_fetch_cover_entry_for_title(
                            provider=provider,
                            platform=platform,
                            title=row["matched_title"],
                            index_path=cover_index_path,
                            existing_entries=cover_entries,
                        )
                    except ProviderError:
                        cover_entry = None
                    if cover_entry:
                        _apply_cover_entry_to_row(row, cover_entry)
                        cover_entries.append(cover_entry)
                row["decision"] = "review"
                row["acquisition_status"] = status
                row["play_status"] = played
            detected_count = len(rows)
            rows = rows[:1] if rows else [
                _blank_review_row(
                    platform=platform,
                    play_status=played,
                    note="No barcode match found; choose a title manually.",
                )
            ]

            write_review(self._audit_path(run_id), rows)

            summary = {
                "provider": provider_name,
                "platform": platform or "",
                "status": status,
                "played": played,
                "uploaded": str(len(image_files)),
                "expected_titles": "1",
                "detected_barcodes": str(detected_count),
                "candidates": str(len(rows)),
                "cover_index_entries": str(len(cover_entries)),
                "barcode_catalog_entries": str(len(barcode_entries)),
                "imported": "0",
                "skipped_existing": "0",
            }
            (run_dir / "summary.csv").write_text(
                "\n".join(f"{key},{value}" for key, value in summary.items()),
                encoding="utf-8",
            )
            self._redirect_remembering_platform(f"/ingest/{run_id}", platform)
        except (PhotoIngestError, ProviderError, ValueError) as exc:
            self._send_html("Upload Photos", self._ingest_form(str(exc), error=True), HTTPStatus.BAD_REQUEST)

    def _summary(self, run_id: str) -> dict[str, str]:
        path = self._run_dir(run_id) / "summary.csv"
        if not path.exists():
            return {}
        summary: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(",")
            summary[key] = value
        return summary

    def _ingest_results(self, run_id: str, message: str | None = None) -> str:
        audit_path = self._audit_path(run_id)
        if not audit_path.exists():
            return _simple_page("Missing Review", "Ingest Run Not Found", "That upload review run no longer exists.")
        rows = read_review(audit_path)
        notice = f'<div class="notice">{_h(message)}</div>' if message else ""
        return f"""
<div class="app-page">
  <section class="page-hero">
    <div class="page-kicker">Confirm Match</div>
    <h1 class="page-title">Review Game</h1>
    <p class="page-subtitle">Confirm the barcode result visually. Change the platform or title if needed, then accept or reject this scan.</p>
  </section>
  {notice}
  <form method="post" action="/ingest/{_h(run_id)}/review">
    {self._review_rows_table(rows)}
  </form>
</div>
"""

    def _uploaded_photo_panel(self, run_id: str) -> str:
        uploads_dir = self._run_dir(run_id) / "uploads"
        photos = sorted(path for path in uploads_dir.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES) if uploads_dir.exists() else []
        if not photos:
            return '<section class="panel"><h2>Uploaded Photos</h2><p class="muted">No uploaded photo files found for this run.</p></section>'
        thumbs = "".join(
            f'<button class="thumb-button" type="button" data-modal-image="/media?path={urllib.parse.quote(str(path))}">'
            f'<img class="uploaded-photo-thumb" src="/media?path={urllib.parse.quote(str(path))}" alt="Uploaded photo {_h(path.name)}">'
            f'</button>'
            for path in photos
        )
        return f"""
<section class="panel">
  <h2>Uploaded Photos</h2>
  <div class="uploaded-photos">{thumbs}</div>
</section>"""

    def _review_rows_table(self, rows: list[dict[str, str]]) -> str:
        cached_platforms = _cached_platform_options(self.platform_options)

        def platform_select(index: int, current_platform: str | None) -> str:
            current = current_platform or ""
            options = list(cached_platforms)
            if current and current not in options:
                options.insert(0, current)
            if not options:
                return (
                    f'<input name="row_{index}_platform" value="{_h(current)}" '
                    f'data-row="{index}" data-role="row-platform-select">'
                )
            option_html = "".join(
                f'<option value="{_h(platform)}"{" selected" if platform == current else ""}>{_h(platform)}</option>'
                for platform in options
            )
            return (
                f'<select name="row_{index}_platform" data-row="{index}" '
                f'data-role="row-platform-select">{option_html}</select>'
            )

        row = rows[0] if rows else _blank_review_row(
            platform=cached_platforms[0] if cached_platforms else "",
            play_status="unplayed",
            note="No barcode match found; choose a title manually.",
        )
        index = 0
        upload = row.get("upload_path") or row.get("photo_path") or ""
        sample_image = row.get("sample_image_path") or row.get("crop_path") or upload
        if not _matched_cover_path(row):
            cover_entry = _cached_exact_cover_entry(
                provider=DEFAULT_PROVIDER,
                platform=row.get("platform") or "",
                title=row.get("matched_title") or "",
            )
            if cover_entry:
                _apply_cover_entry_to_row(row, cover_entry)
        matched_cover = _matched_cover_path(row)
        sample_html = _cover_thumb(sample_image, "Uploaded", "Uploaded source image", opens_modal=True)
        matched_cover_html = _cover_thumb(
            matched_cover,
            "Matched",
            "Matched cached cover art",
            row_index=index,
            role="matched-cover-sample",
        )
        barcode_text = row.get("barcode") or ""
        source_text = f"Barcode: {barcode_text}" if barcode_text else "No barcode catalog match"
        hidden_metadata = "".join(
            f'<input type="hidden" name="row_{index}_{field}" value="{_h(row.get(field))}">'
            for field in (
                "provider",
                "provider_game_id",
                "candidate_title",
                "release_date",
                "developer",
                "publisher",
                "description",
                "cover_url",
                "confidence",
                "notes",
                "barcode",
                "source_provider",
                "source_id",
            )
        )
        play_status = row.get("play_status") if row.get("play_status") in PLAY_STATUSES else "unplayed"
        acquisition_status = (
            row.get("acquisition_status")
            if row.get("acquisition_status") in OWNERSHIP_STATUSES
            else "owned"
        )
        ownership_options = "".join(
            f'<option value="{status}"{_selected(acquisition_status, status)}>{status.replace("_", " ").title()}</option>'
            for status in OWNERSHIP_STATUSES
        )
        play_options = "".join(
            f'<option value="{status}"{_selected(play_status, status)}>{status.title()}</option>'
            for status in PLAY_STATUSES
        )

        return f"""
<input type="hidden" name="row_count" value="1">
<section class="panel single-review">
  <div class="single-review-covers">
    {sample_html}
    {matched_cover_html}
  </div>
  <div class="single-review-fields">
    <p class="muted">Source: {_h(source_text)}</p>
    <label>Platform
      {platform_select(index, row.get('platform'))}
    </label>
    <label>Matched Title
      <div class="match-control">
        <textarea class="title-field" name="row_{index}_matched_title" rows="2" data-row="{index}" data-role="match-title-input" autocomplete="off" placeholder="Start typing to choose a title">{_h(row.get('matched_title'))}</textarea>
        <div class="match-suggestions" data-row="{index}" data-role="match-suggestions" hidden></div>
      </div>
    </label>
    <div class="review-state-grid">
      <label>Collection State
        <select name="row_{index}_acquisition_status">{ownership_options}</select>
      </label>
      <label>Played State
        <select name="row_{index}_play_status">{play_options}</select>
      </label>
    </div>
    <input type="hidden" name="row_{index}_upload_path" value="{_h(upload)}">
    <input type="hidden" name="row_{index}_sample_image_path" value="{_h(sample_image)}">
    <input type="hidden" name="row_{index}_decision" value="review">
    {hidden_metadata}
    <div class="single-review-actions">
      <button type="submit" name="row_{index}_decision" value="accept" aria-label="Accept match"><span class="button-icon">&#10003;</span>Accept</button>
      <button type="submit" class="reject" name="row_{index}_decision" value="ignore" formnovalidate aria-label="Reject match"><span class="button-icon">&#10005;</span>Reject</button>
    </div>
  </div>
</section>
"""

    def _handle_ingest_review(self, run_id: str) -> None:
        form = self._form()
        row_count = int(form.get("row_count") or "0")
        rows: list[dict[str, str]] = []
        for index in range(row_count):
            row = {}
            for field in INTAKE_FIELDS:
                row[field] = form.get(f"row_{index}_{field}", "")
            rows.append(row)
        write_review(self._audit_path(run_id), rows)
        summary = self._summary(run_id)
        duplicates = find_duplicate_accepted_rows(db_path=self.db_path, rows=rows)
        reviewed_platform = next((row.get("platform") for row in rows if row.get("platform")), "")
        if duplicates:
            duplicate = duplicates[0]
            platform = f" ({duplicate.platform})" if duplicate.platform else ""
            status = duplicate.acquisition_status.replace("_", " ")
            message = (
                f"Duplicate blocked: {duplicate.title}{platform} is already in your library "
                f"as {status}; play status is {duplicate.play_status}."
            )
            if reviewed_platform:
                self._redirect_remembering_platform(f"/ingest?message={urllib.parse.quote(message)}", reviewed_platform)
            else:
                self._redirect(f"/ingest?message={urllib.parse.quote(message)}")
            return
        imported, skipped = import_accepted_rows(
            db_path=self.db_path,
            rows=rows,
            status=summary.get("status") or "owned",
            played=summary.get("played") or "unplayed",
            skip_existing=True,
        )
        message = (
            "Duplicate blocked: this game is already in your library."
            if skipped
            else f"Imported {imported}; skipped {skipped}."
        )
        if reviewed_platform:
            self._redirect_remembering_platform(f"/ingest?message={urllib.parse.quote(message)}", reviewed_platform)
        else:
            self._redirect(f"/ingest?message={urllib.parse.quote(message)}")

    def _rows_table(self, rows: list[sqlite3.Row]) -> str:
        if not rows:
            return '<div class="panel muted">No games match this view.</div>'
        table_rows = []
        for row in rows:
            owning_class = " sold" if row["acquisition_status"] == "sold" else ""
            platform = _h(row["platform"]) if row["platform"] else ""
            table_rows.append(
                f"""
<tr>
  <td><a class="title-link" href="/games/{row['game_id']}">{_h(row['title'])}</a></td>
  <td>{platform}</td>
  <td><span class="badge{owning_class}">{_h(row['acquisition_status'])}</span></td>
  <td><span class="badge">{_h(row['latest_play_status'])}</span></td>
  <td class="muted">{_h(row['provider'])}:{_h(row['provider_game_id'])}</td>
</tr>"""
            )
        return f"""
<table>
  <thead><tr><th>Title</th><th>Platform</th><th>Ownership</th><th>Played</th><th>Provider</th></tr></thead>
  <tbody>{''.join(table_rows)}</tbody>
</table>"""

    def _game_detail(self, game_id: int) -> str:
        conn = db.connect(self.db_path)
        try:
            row = db.get_game_detail(conn, game_id=game_id)
        finally:
            conn.close()
        if row is None:
            return _simple_page("Missing Game", "Game Not Found", "That game is not in the local collection database.")
        cover = (
            f'<img class="cover" src="{_h(row["cover_url"])}" alt="Cover art for {_h(row["title"])}">'
            if row["cover_url"]
            else '<div class="cover placeholder">No cover art</div>'
        )
        ownership_options = "".join(
            f'<option value="{status}"{_selected(row["acquisition_status"], status)}>{status.replace("_", " ").title()}</option>'
            for status in OWNERSHIP_STATUSES
        )
        play_options = "".join(
            f'<option value="{status}"{_selected(row["latest_play_status"], status)}>{status.title()}</option>'
            for status in PLAY_STATUSES
        )
        return f"""
<div class="app-page">
  <section class="page-hero">
    <div class="page-kicker">Game Detail</div>
    <h1 class="page-title">{_h(row['title'])}</h1>
    <p class="page-subtitle">{_h(row['platform']) or 'No platform'}{(' | ' + _h(row['publisher'])) if row['publisher'] else ''}</p>
  </section>
  <div class="detail">
    <div class="cover-stack">
      {cover}
      <section class="panel">
        <dl class="metadata-list">
          <div><dt>Release date</dt><dd>{_h(row['release_date']) or 'Unknown'}</dd></div>
          <div class="metadata-description"><dt>Description</dt><dd>{_h(row['description']) or 'No description available.'}</dd></div>
        </dl>
        <div class="actions">
          <a class="button secondary" href="/" aria-label="Back to library"><span class="button-icon">&#8592;</span>Back</a>
          <form method="post" action="/items/{row['collection_item_id']}/delete" onsubmit="return confirm('Delete this game from your library? This removes the collection copy but keeps historical game metadata.');">
            <button class="danger" type="submit" aria-label="Delete from library"><span class="button-icon">&#128465;</span>Delete</button>
          </form>
        </div>
      </section>
    </div>
    <div class="page-stack">
      <section class="panel">
        <form method="post" action="/items/{row['collection_item_id']}/collection">
          <input type="hidden" name="game_id" value="{game_id}">
          <div class="review-state-grid">
            <label>Collection State
              <select name="acquisition_status">{ownership_options}</select>
            </label>
            <label>Played State
              <select name="play_status">{play_options}</select>
            </label>
          </div>
          <div class="actions"><button type="submit"><span class="button-icon">&#10003;</span>Save</button></div>
        </form>
      </section>
    </div>
  </div>
</div>"""


def serve(
    db_path: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    prebuild_cover_indexes: bool = True,
    refresh_cover_indexes: bool = False,
    refresh_platform_cache: bool = False,
) -> None:
    db.init_db(db_path)
    platform_options = PLATFORM_PRESETS
    try:
        provider = get_provider("igdb")
        platform_options = build_platform_cache(provider=provider, refresh=refresh_platform_cache)
        if prebuild_cover_indexes:
            def prebuild() -> None:
                print("Prebuilding prioritized cover indexes in the background...")
                try:
                    for platform, count in prebuild_prioritized_cover_indexes(
                        provider=provider,
                        limit=None,
                        refresh=refresh_cover_indexes,
                    ).items():
                        print(f"  {platform}: {count} covers indexed")
                except ProviderError as exc:
                    print(f"Warning: background cover-index prebuild failed: {exc}")

            threading.Thread(target=prebuild, daemon=True).start()
    except ProviderError as exc:
        print(f"Warning: could not prebuild IGDB caches: {exc}")
    handler = type(
        "ConfiguredCollectionHandler",
        (CollectionHandler,),
        {"db_path": db_path, "platform_options": platform_options},
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Serving game collection at http://{host}:{port}")
    server.serve_forever()
