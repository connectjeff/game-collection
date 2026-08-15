from __future__ import annotations

import csv
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .cover_cache import CoverIndexEntry, slugify
from .providers import GameMatch


BARCODE_CATALOG_PATH = Path("review/barcodes/catalog.csv")
BARCODE_CACHE_ROOT = Path("review/barcodes")
BARCODE_CATALOG_FIELDS = [
    "barcode",
    "title",
    "platform",
    "provider",
    "provider_game_id",
    "release_date",
    "developer",
    "publisher",
    "description",
    "cover_url",
]


@dataclass(frozen=True)
class BarcodeCatalogEntry:
    barcode: str
    title: str
    platform: str | None = None
    provider: str | None = None
    provider_game_id: str | None = None
    release_date: str | None = None
    developer: str | None = None
    publisher: str | None = None
    description: str | None = None
    cover_url: str | None = None


@dataclass(frozen=True)
class BarcodeFormatHint:
    platform_family: str
    accepted_lengths: tuple[int, ...]
    preferred_prefixes: tuple[str, ...] = ()
    notes: str = ""


DEFAULT_BARCODE_HINT = BarcodeFormatHint(
    platform_family="retail",
    accepted_lengths=(8, 12, 13, 14),
    notes="Retail games usually use GS1 GTIN-12/UPC-A or GTIN-13/EAN/JAN; UPC-E and GTIN-14 are accepted as fallbacks.",
)

PLATFORM_BARCODE_HINTS = {
    "nintendo": BarcodeFormatHint(
        platform_family="nintendo",
        accepted_lengths=(8, 12, 13, 14),
        preferred_prefixes=("045496", "4902370"),
        notes="Nintendo-published North American retail releases commonly use UPC prefix 045496; Japanese JANs commonly begin 4902370.",
    ),
    "playstation": BarcodeFormatHint(
        platform_family="playstation",
        accepted_lengths=(8, 12, 13, 14),
        preferred_prefixes=("711719", "071171", "4948872"),
        notes="Sony Interactive Entertainment retail releases commonly use UPC prefix 711719/071171 in North America and JAN prefix 4948872 in Japan.",
    ),
    "xbox": BarcodeFormatHint(
        platform_family="xbox",
        accepted_lengths=(8, 12, 13, 14),
        preferred_prefixes=("885370", "889842", "0885370"),
        notes="Microsoft Xbox retail releases commonly use UPC prefixes from Microsoft ranges such as 885370 and 889842.",
    ),
}

GAME_PUBLISHER_PREFIXES = (
    "008888",  # Ubisoft
    "010086",  # Sega of America
    "013388",  # Capcom
    "014633",  # Electronic Arts
    "045496",  # Nintendo of America
    "047875",  # Activision
    "071171",  # Sony / PlayStation
    "093155",  # Bethesda
    "662248",  # Square Enix
    "710425",  # Take-Two / 2K
    "711719",  # Sony / PlayStation
    "812303",  # Limited Run Games
    "883929",  # Warner Bros.
    "885370",  # Microsoft
    "889842",  # Microsoft
    "4902370",  # Nintendo Japan
    "4948872",  # Sony Japan
    "4974365",  # Sega Japan
)

BARCODE_COLUMN_ALIASES = ("barcode", "upc", "ean", "gtin", "product-code", "product_code", "identifier")
TITLE_COLUMN_ALIASES = ("title", "product-name", "product_name", "name", "game", "game-title", "game_title")
PLATFORM_COLUMN_ALIASES = ("platform", "console-name", "console_name", "console", "system", "platform-name", "platform_name")
PROVIDER_ID_COLUMN_ALIASES = ("id", "product-id", "product_id", "provider_game_id", "pricecharting-id")
PROVIDER_COLUMN_ALIASES = ("provider", "source", "data-source", "data_source")
RELEASE_DATE_COLUMN_ALIASES = ("release-date", "release_date", "released")
PUBLISHER_COLUMN_ALIASES = ("publisher", "publishers")
DEVELOPER_COLUMN_ALIASES = ("developer", "developers")
COVER_URL_COLUMN_ALIASES = ("cover_url", "cover-url", "image", "image-url", "image_url")


def platform_barcode_hint(platform: str | None) -> BarcodeFormatHint:
    normalized = (platform or "").casefold()
    if any(token in normalized for token in ("nintendo", "switch", "wii", "game boy", "gamecube", "ds", "3ds")):
        return PLATFORM_BARCODE_HINTS["nintendo"]
    if any(token in normalized for token in ("playstation", "ps1", "ps2", "ps3", "ps4", "ps5", "psp", "vita")):
        return PLATFORM_BARCODE_HINTS["playstation"]
    if "xbox" in normalized:
        return PLATFORM_BARCODE_HINTS["xbox"]
    return DEFAULT_BARCODE_HINT


def is_expected_barcode_for_platform(value: str, platform: str | None) -> bool:
    normalized = normalize_barcode(value)
    return len(normalized) in platform_barcode_hint(platform).accepted_lengths and is_valid_gtin(normalized)


def _barcode_sort_key(value: str, platform: str | None) -> tuple[int, str]:
    normalized = normalize_barcode(value)
    hint = platform_barcode_hint(platform)
    preferred = any(normalized.startswith(prefix) for prefix in hint.preferred_prefixes)
    game_publisher = any(normalized.startswith(prefix) for prefix in GAME_PUBLISHER_PREFIXES)
    return (0 if preferred else 1 if game_publisher else 2, normalized)


def normalize_barcode(value: str) -> str:
    digits = re.sub(r"\D+", "", value)
    while len(digits) > 12 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def gtin_check_digit(body: str) -> int:
    total = 0
    for index, char in enumerate(reversed(body)):
        total += int(char) * (3 if index % 2 == 0 else 1)
    return (10 - (total % 10)) % 10


def is_valid_gtin(value: str) -> bool:
    digits = re.sub(r"\D+", "", value)
    if len(digits) not in (8, 12, 13, 14):
        return False
    return gtin_check_digit(digits[:-1]) == int(digits[-1])


def barcode_variants(value: str) -> set[str]:
    normalized = normalize_barcode(value)
    variants = {normalized}
    if len(normalized) == 8:
        variants.add(f"000000{normalized}")
    if len(normalized) == 12:
        variants.add(f"0{normalized}")
        variants.add(f"00{normalized}")
    if len(normalized) == 13:
        variants.add(f"0{normalized}")
    return {item for item in variants if item}


def default_barcode_cache_path(platform: str | None) -> Path:
    if platform:
        return BARCODE_CACHE_ROOT / slugify(platform) / "catalog.csv"
    return BARCODE_CATALOG_PATH


def read_barcode_catalog(path: Path = BARCODE_CATALOG_PATH) -> list[BarcodeCatalogEntry]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        return [
            BarcodeCatalogEntry(
                barcode=normalize_barcode(row.get("barcode", "")),
                title=row.get("title", ""),
                platform=row.get("platform") or None,
                provider=row.get("provider") or None,
                provider_game_id=row.get("provider_game_id") or None,
                release_date=row.get("release_date") or None,
                developer=row.get("developer") or None,
                publisher=row.get("publisher") or None,
                description=row.get("description") or None,
                cover_url=row.get("cover_url") or None,
            )
            for row in rows
            if is_valid_gtin(normalize_barcode(row.get("barcode", ""))) and row.get("title")
        ]


def read_platform_barcode_cache(platform: str | None) -> list[BarcodeCatalogEntry]:
    platform_path = default_barcode_cache_path(platform)
    platform_entries = read_barcode_catalog(platform_path)
    if platform_entries:
        return platform_entries
    return [
        entry
        for entry in read_barcode_catalog(BARCODE_CATALOG_PATH)
        if not platform or not entry.platform or entry.platform.casefold() == platform.casefold()
    ]


def write_barcode_catalog(path: Path, entries: list[BarcodeCatalogEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=BARCODE_CATALOG_FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {
                    "barcode": entry.barcode,
                    "title": entry.title,
                    "platform": entry.platform or "",
                    "provider": entry.provider or "",
                    "provider_game_id": entry.provider_game_id or "",
                    "release_date": entry.release_date or "",
                    "developer": entry.developer or "",
                    "publisher": entry.publisher or "",
                    "description": entry.description or "",
                    "cover_url": entry.cover_url or "",
                }
            )


def build_barcode_cache(
    *,
    source_paths: list[str | Path],
    platforms: list[str] | None = None,
    cache_root: Path = BARCODE_CACHE_ROOT,
    provider: str | None = None,
) -> dict[str, int]:
    entries = _dedupe_barcode_entries(
        _with_provider(entry, provider)
        for source_path in source_paths
        for entry in _read_barcode_sources(source_path)
    )
    if platforms:
        platform_keys = {platform.casefold() for platform in platforms}
        entries = [
            entry
            for entry in entries
            if (entry.platform or "").casefold() in platform_keys
        ]

    grouped: dict[str, list[BarcodeCatalogEntry]] = {}
    for entry in entries:
        if not entry.platform:
            continue
        grouped.setdefault(entry.platform, []).append(entry)

    results: dict[str, int] = {}
    for platform, platform_entries in sorted(grouped.items(), key=lambda item: item[0].casefold()):
        out_path = cache_root / slugify(platform) / "catalog.csv"
        write_barcode_catalog(out_path, platform_entries)
        results[platform] = len(platform_entries)

    merged_path = cache_root / "catalog.csv"
    write_barcode_catalog(merged_path, entries)
    results["all"] = len(entries)
    return results


def barcode_cache_statuses(platforms: list[str]) -> dict[str, int]:
    return {platform: len(read_barcode_catalog(default_barcode_cache_path(platform))) for platform in platforms}


def _read_barcode_sources(path: str | Path) -> list[BarcodeCatalogEntry]:
    if isinstance(path, str) and _is_url(path):
        return _read_external_barcode_catalog_from_text(_read_url_text(path))
    path = Path(path)
    if path.is_dir():
        entries: list[BarcodeCatalogEntry] = []
        for item in sorted(path.glob("*.csv")):
            entries.extend(_read_external_barcode_catalog(item))
        return entries
    return _read_external_barcode_catalog(path)


def _is_url(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")


def _read_url_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "game-collection/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8-sig")


def _read_external_barcode_catalog(path: Path) -> list[BarcodeCatalogEntry]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return _read_external_barcode_catalog_from_rows(csv.DictReader(handle))


def _read_external_barcode_catalog_from_text(text: str) -> list[BarcodeCatalogEntry]:
    return _read_external_barcode_catalog_from_rows(csv.DictReader(text.splitlines()))


def _read_external_barcode_catalog_from_rows(rows: Iterable[dict[str, str]]) -> list[BarcodeCatalogEntry]:
    entries: list[BarcodeCatalogEntry] = []
    for row in rows:
        normalized_row = {str(key).strip().casefold(): value for key, value in row.items() if key is not None}
        barcode = normalize_barcode(_first_value(normalized_row, BARCODE_COLUMN_ALIASES))
        title = _first_value(normalized_row, TITLE_COLUMN_ALIASES).strip()
        if not barcode or not title or not is_valid_gtin(barcode):
            continue
        entries.append(
            BarcodeCatalogEntry(
                barcode=barcode,
                title=title,
                platform=_first_value(normalized_row, PLATFORM_COLUMN_ALIASES).strip() or None,
                provider=_first_value(normalized_row, PROVIDER_COLUMN_ALIASES).strip() or None,
                provider_game_id=_first_value(normalized_row, PROVIDER_ID_COLUMN_ALIASES).strip() or None,
                release_date=_first_value(normalized_row, RELEASE_DATE_COLUMN_ALIASES).strip() or None,
                developer=_first_value(normalized_row, DEVELOPER_COLUMN_ALIASES).strip() or None,
                publisher=_first_value(normalized_row, PUBLISHER_COLUMN_ALIASES).strip() or None,
                cover_url=_first_value(normalized_row, COVER_URL_COLUMN_ALIASES).strip() or None,
            )
        )
    return entries


def _first_value(row: dict[str, str], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        if alias in row and row[alias] is not None:
            return str(row[alias])
    return ""


def _with_provider(entry: BarcodeCatalogEntry, provider: str | None) -> BarcodeCatalogEntry:
    if not provider or entry.provider:
        return entry
    return BarcodeCatalogEntry(
        barcode=entry.barcode,
        title=entry.title,
        platform=entry.platform,
        provider=provider,
        provider_game_id=entry.provider_game_id,
        release_date=entry.release_date,
        developer=entry.developer,
        publisher=entry.publisher,
        description=entry.description,
        cover_url=entry.cover_url,
    )


def _dedupe_barcode_entries(entries: list[BarcodeCatalogEntry] | Any) -> list[BarcodeCatalogEntry]:
    deduped: dict[tuple[str, str], BarcodeCatalogEntry] = {}
    for entry in entries:
        barcode = normalize_barcode(entry.barcode)
        if not is_valid_gtin(barcode):
            continue
        platform = entry.platform or ""
        key = (barcode, platform.casefold())
        deduped[key] = BarcodeCatalogEntry(
            barcode=barcode,
            title=entry.title,
            platform=entry.platform,
            provider=entry.provider,
            provider_game_id=entry.provider_game_id,
            release_date=entry.release_date,
            developer=entry.developer,
            publisher=entry.publisher,
            description=entry.description,
            cover_url=entry.cover_url,
        )
    return sorted(deduped.values(), key=lambda item: ((item.platform or "").casefold(), item.title.casefold(), item.barcode))


def detect_barcodes(image_path: Path, *, platform: str | None = None) -> list[str]:
    import cv2  # type: ignore[import-not-found]

    image = cv2.imread(str(image_path))
    if image is None:
        return []
    detector = cv2.barcode_BarcodeDetector()
    decoded_values: list[str] = []
    for candidate in _barcode_decode_variants(cv2, detector, image):
        for value in candidate:
            normalized = normalize_barcode(str(value))
            if (
                normalized
                and is_expected_barcode_for_platform(normalized, platform)
                and normalized not in decoded_values
            ):
                decoded_values.append(normalized)
    return sorted(decoded_values, key=lambda value: _barcode_sort_key(value, platform))


def _barcode_decode_variants(cv2: Any, detector: Any, image: Any) -> list[tuple[str, ...]]:
    variants = [image]
    variants.append(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
    variants.append(cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
    decoded: list[tuple[str, ...]] = []
    for variant in variants:
        try:
            ok, values, _types, _points = detector.detectAndDecodeWithType(variant)
        except (cv2.error, ValueError):
            continue
        if ok and values:
            decoded.append(tuple(str(value) for value in values if value))
    return decoded


def match_barcode(
    barcode: str,
    catalog: list[BarcodeCatalogEntry],
    *,
    platform: str | None = None,
    cover_entries: list[CoverIndexEntry] | None = None,
) -> GameMatch | None:
    requested = barcode_variants(barcode)
    platform_key = (platform or "").casefold()
    candidates = [entry for entry in catalog if barcode_variants(entry.barcode) & requested]
    if platform_key:
        platform_candidates = [
            entry for entry in candidates if not entry.platform or entry.platform.casefold() == platform_key
        ]
        candidates = platform_candidates or candidates
    if not candidates:
        return None
    entry = candidates[0]
    indexed = _cover_entry_for_catalog_entry(entry, cover_entries or [])
    provider = entry.provider or (indexed.provider if indexed else "barcode")
    provider_game_id = entry.provider_game_id or (indexed.provider_game_id if indexed else normalize_barcode(barcode))
    cover_url = entry.cover_url or (indexed.cover_url if indexed else None)
    return GameMatch(
        provider=provider,
        provider_game_id=provider_game_id,
        title=entry.title,
        platform=entry.platform or platform,
        release_date=entry.release_date or (indexed.release_date if indexed else None),
        developer=entry.developer or (indexed.developer if indexed else None),
        publisher=entry.publisher or (indexed.publisher if indexed else None),
        description=entry.description or (indexed.description if indexed else None),
        cover_url=cover_url,
        confidence=1.0,
        raw={"barcode": normalize_barcode(barcode), "match_type": "barcode"},
    )


def _cover_entry_for_catalog_entry(
    catalog_entry: BarcodeCatalogEntry,
    cover_entries: list[CoverIndexEntry],
) -> CoverIndexEntry | None:
    title_key = _normalize_title(catalog_entry.title)
    platform_key = (catalog_entry.platform or "").casefold()
    for entry in cover_entries:
        if _normalize_title(entry.title) != title_key:
            continue
        if platform_key and (entry.platform or "").casefold() != platform_key:
            continue
        return entry
    return None


def _normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())
