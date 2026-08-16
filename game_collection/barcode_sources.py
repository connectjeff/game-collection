from __future__ import annotations

import csv
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .barcode_match import (
    BarcodeCatalogEntry,
    _read_external_barcode_catalog_from_text,
    normalize_barcode,
    normalize_platform_name,
    read_barcode_catalog,
    write_barcode_catalog,
)


USER_AGENT = "game-collection/0.1 (local barcode cache builder)"
WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
UPCDEV_BASE_URL = "https://upc.dev/v1"
OPEN_PRODUCTS_FACTS_BASE_URL = "https://world.openproductsfacts.org/api/v2"


class BarcodeSourceError(RuntimeError):
    pass


def download_wikidata_video_game_barcodes(
    out_path: Path,
    *,
    limit: int | None = None,
    offset: int | None = None,
    incremental: bool = False,
) -> list[BarcodeCatalogEntry]:
    existing = read_barcode_catalog(out_path) if incremental and out_path.exists() else []
    if incremental and offset is None and limit is not None:
        offset = len(existing)
    limit_clause = f"LIMIT {limit}" if limit else ""
    offset_clause = f"OFFSET {offset}" if offset else ""
    query = f"""
SELECT ?item ?itemLabel ?gtin ?platformLabel WHERE {{
  ?item wdt:P3962 ?gtin.
  OPTIONAL {{ ?item wdt:P400 ?platform. }}
  FILTER(EXISTS {{ ?item wdt:P31 wd:Q7889. }} || BOUND(?platform))
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
{limit_clause}
{offset_clause}
"""
    payload = _get_json(
        f"{WIKIDATA_SPARQL_ENDPOINT}?{urllib.parse.urlencode({'query': query, 'format': 'json'})}"
    )
    bindings = payload.get("results", {}).get("bindings", [])
    entries: list[BarcodeCatalogEntry] = []
    for binding in bindings:
        barcode = normalize_barcode(_binding_value(binding, "gtin"))
        title = _binding_value(binding, "itemLabel")
        if not barcode or not title:
            continue
        entries.append(
            BarcodeCatalogEntry(
                barcode=barcode,
                title=title,
                platform=_binding_value(binding, "platformLabel") or None,
                provider="wikidata",
                provider_game_id=_binding_value(binding, "item").rsplit("/", 1)[-1],
            )
        )
    merged = _merge_entries(existing, entries)
    write_barcode_catalog(out_path, merged)
    return merged


def download_upcdev_products(out_path: Path, *, barcodes: list[str], incremental: bool = False) -> list[BarcodeCatalogEntry]:
    existing = read_barcode_catalog(out_path) if incremental and out_path.exists() else []
    entries: list[BarcodeCatalogEntry] = []
    for barcode in barcodes:
        normalized = normalize_barcode(barcode)
        if not normalized:
            continue
        try:
            payload = _get_json(f"{UPCDEV_BASE_URL}/product/{urllib.parse.quote(normalized)}")
        except BarcodeSourceError:
            continue
        product = payload.get("data") or {}
        entry = _upcdev_product_to_entry(product)
        if entry:
            entries.append(entry)
    merged = _merge_entries(existing, entries)
    write_barcode_catalog(out_path, merged)
    return merged


def download_upcdev_search(out_path: Path, *, query: str, incremental: bool = False) -> list[BarcodeCatalogEntry]:
    existing = read_barcode_catalog(out_path) if incremental and out_path.exists() else []
    payload = _get_json(f"{UPCDEV_BASE_URL}/search?{urllib.parse.urlencode({'q': query})}")
    products = payload.get("data", {}).get("products") or []
    entries = [entry for product in products if (entry := _upcdev_product_to_entry(product))]
    merged = _merge_entries(existing, entries)
    write_barcode_catalog(out_path, merged)
    return merged


def download_open_products_facts_products(
    out_path: Path,
    *,
    barcodes: list[str],
    incremental: bool = False,
) -> list[BarcodeCatalogEntry]:
    existing = read_barcode_catalog(out_path) if incremental and out_path.exists() else []
    entries: list[BarcodeCatalogEntry] = []
    for barcode in barcodes:
        normalized = normalize_barcode(barcode)
        if not normalized:
            continue
        try:
            payload = _get_json(f"{OPEN_PRODUCTS_FACTS_BASE_URL}/product/{urllib.parse.quote(normalized)}.json")
        except BarcodeSourceError:
            continue
        product = payload.get("product") or {}
        if not product:
            continue
        title = product.get("product_name") or product.get("generic_name") or product.get("abbreviated_product_name")
        if not title:
            continue
        entries.append(
            BarcodeCatalogEntry(
                barcode=normalized,
                title=str(title),
                platform=_platform_from_text(" ".join(str(product.get(key) or "") for key in ("categories", "labels", "tags"))),
                provider="openproductsfacts",
                provider_game_id=normalized,
                publisher=str(product.get("brands") or "") or None,
                description=str(product.get("generic_name") or "") or None,
                cover_url=str(product.get("image_front_url") or product.get("image_url") or "") or None,
            )
        )
    merged = _merge_entries(existing, entries)
    write_barcode_catalog(out_path, merged)
    return merged


def lookup_live_barcode(barcode: str, *, platform: str | None = None) -> BarcodeCatalogEntry | None:
    normalized = normalize_barcode(barcode)
    if not normalized:
        return None
    for lookup in (
        _lookup_pricecharting_redirect,
        _lookup_upcdev_product,
        _lookup_open_products_facts_product,
    ):
        entry = lookup(normalized, platform=platform)
        if entry:
            return entry
    return None


def download_csv_url(out_path: Path, *, url: str, incremental: bool = False) -> list[BarcodeCatalogEntry]:
    existing = read_barcode_catalog(out_path) if incremental and out_path.exists() else []
    entries = _read_external_barcode_catalog_from_text(_read_url_text(url))
    merged = _merge_entries(existing, entries)
    write_barcode_catalog(out_path, merged)
    return merged


def read_barcodes_file(path: Path) -> list[str]:
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            values.append(stripped)
    return values


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BarcodeSourceError(f"Barcode source request failed: {url}") from exc


def _read_url_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/csv,*/*",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8-sig")
    except OSError as exc:
        raise BarcodeSourceError(f"Barcode CSV request failed: {url}") from exc


def _merge_entries(existing: list[BarcodeCatalogEntry], incoming: list[BarcodeCatalogEntry]) -> list[BarcodeCatalogEntry]:
    merged: dict[tuple[str, str], BarcodeCatalogEntry] = {}
    for entry in [*existing, *incoming]:
        key = (normalize_barcode(entry.barcode), (entry.platform or "").casefold())
        if key[0]:
            merged[key] = entry
    return sorted(merged.values(), key=lambda entry: ((entry.platform or "").casefold(), entry.title.casefold(), entry.barcode))


def _binding_value(binding: dict[str, Any], key: str) -> str:
    value = binding.get(key, {})
    return str(value.get("value") or "")


def _upcdev_product_to_entry(product: dict[str, Any]) -> BarcodeCatalogEntry | None:
    barcode = normalize_barcode(str(product.get("upc") or product.get("barcode") or product.get("gtin") or ""))
    title = str(product.get("name") or product.get("title") or "").strip()
    if not barcode or not title:
        return None
    category = str(product.get("category") or "")
    return BarcodeCatalogEntry(
        barcode=barcode,
        title=title,
        platform=_platform_from_text(f"{title} {category}"),
        provider="upcdev",
        provider_game_id=barcode,
        publisher=str(product.get("brand") or "") or None,
        description=str(product.get("description") or "") or None,
        cover_url=str(product.get("image_url") or "") or None,
    )


def _lookup_upcdev_product(barcode: str, *, platform: str | None = None) -> BarcodeCatalogEntry | None:
    try:
        payload = _get_json(f"{UPCDEV_BASE_URL}/product/{urllib.parse.quote(barcode)}")
    except BarcodeSourceError:
        return None
    product = payload.get("data") or {}
    entry = _upcdev_product_to_entry(product)
    entry = _with_platform_fallback(entry, platform)
    return entry if _is_likely_video_game_product(entry) else None


def _lookup_open_products_facts_product(barcode: str, *, platform: str | None = None) -> BarcodeCatalogEntry | None:
    try:
        payload = _get_json(f"{OPEN_PRODUCTS_FACTS_BASE_URL}/product/{urllib.parse.quote(barcode)}.json")
    except BarcodeSourceError:
        return None
    product = payload.get("product") or {}
    title = product.get("product_name") or product.get("generic_name") or product.get("abbreviated_product_name")
    if not title:
        return None
    entry = BarcodeCatalogEntry(
        barcode=barcode,
        title=str(title),
        platform=_platform_from_text(" ".join(str(product.get(key) or "") for key in ("categories", "labels", "tags"))),
        provider="openproductsfacts",
        provider_game_id=barcode,
        publisher=str(product.get("brands") or "") or None,
        description=str(product.get("generic_name") or "") or None,
        cover_url=str(product.get("image_front_url") or product.get("image_url") or "") or None,
    )
    entry = _with_platform_fallback(entry, platform)
    return entry if _is_likely_video_game_product(entry) else None


def _lookup_pricecharting_redirect(barcode: str, *, platform: str | None = None) -> BarcodeCatalogEntry | None:
    url = f"https://www.pricecharting.com/search-products?{urllib.parse.urlencode({'type': 'videogames', 'q': barcode})}"
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            location = response.geturl()
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") or ""
    except OSError:
        return None
    parsed = urllib.parse.urlparse(location)
    if "category=no-results" in location or "/game/" not in parsed.path:
        return None
    match = re.search(r"/game/([^/]+)/([^/?#]+)", parsed.path)
    if not match:
        return None
    system_slug, title_slug = match.groups()
    resolved_platform = _pricecharting_platform(system_slug) or normalize_platform_name(platform)
    title = _title_from_slug(title_slug)
    if not title:
        return None
    return BarcodeCatalogEntry(
        barcode=barcode,
        title=title,
        platform=resolved_platform,
        provider="pricecharting-public",
        provider_game_id=location or barcode,
    )


def _with_platform_fallback(entry: BarcodeCatalogEntry | None, platform: str | None) -> BarcodeCatalogEntry | None:
    if not entry:
        return None
    resolved_platform = normalize_platform_name(entry.platform) or normalize_platform_name(platform)
    return BarcodeCatalogEntry(
        barcode=entry.barcode,
        title=entry.title,
        platform=resolved_platform,
        provider=entry.provider,
        provider_game_id=entry.provider_game_id,
        release_date=entry.release_date,
        developer=entry.developer,
        publisher=entry.publisher,
        description=entry.description,
        cover_url=entry.cover_url,
    )


def _is_likely_video_game_product(entry: BarcodeCatalogEntry | None) -> bool:
    if not entry:
        return False
    text = " ".join(
        value
        for value in (
            entry.title,
            entry.platform or "",
            entry.publisher or "",
            entry.description or "",
        )
        if value
    ).casefold()
    hardware_terms = (
        "console",
        "controller",
        "joy-con",
        "joy con",
        "headset",
        "charging",
        "dock",
        "system",
        "handheld play",
        "memory card",
        "amiibo",
    )
    if any(term in text for term in hardware_terms):
        return False
    game_terms = (
        "video game",
        "game",
        "nintendo switch",
        "playstation",
        "ps4",
        "ps5",
        "xbox",
        "wii",
        "gamecube",
    )
    return any(term in text for term in game_terms)


def _pricecharting_platform(slug: str) -> str | None:
    aliases = {
        "nintendo-switch": "Nintendo Switch",
        "playstation-5": "PlayStation 5",
        "playstation-4": "PlayStation 4",
        "xbox-one": "Xbox One",
        "xbox-series-x": "Xbox Series X|S",
        "xbox-series-s": "Xbox Series X|S",
        "nintendo-gamecube": "Nintendo GameCube",
        "wii": "Wii",
        "wii-u": "Wii U",
        "nintendo-ds": "Nintendo DS",
        "nintendo-3ds": "Nintendo 3DS",
        "playstation-3": "PlayStation 3",
        "playstation-2": "PlayStation 2",
        "playstation-vita": "PlayStation Vita",
    }
    return aliases.get(slug.casefold())


def _title_from_slug(slug: str) -> str:
    words = urllib.parse.unquote(slug).replace("-", " ").split()
    small_words = {"a", "an", "and", "for", "of", "or", "the", "to", "with"}
    titled = []
    for index, word in enumerate(words):
        upper = word.upper()
        if upper in {"ii", "iii", "iv", "vi", "vii", "viii", "ix", "x", "xl"}:
            titled.append(upper)
        elif index > 0 and word.casefold() in small_words:
            titled.append(word.casefold())
        else:
            titled.append(word[:1].upper() + word[1:])
    return " ".join(titled)


def _platform_from_text(text: str) -> str | None:
    lowered = text.casefold()
    platform_aliases = [
        ("Nintendo Switch", ("nintendo switch", "switch")),
        ("PlayStation 5", ("playstation 5", "ps5")),
        ("PlayStation 4", ("playstation 4", "ps4")),
        ("Xbox Series X|S", ("xbox series x", "xbox series s", "series x", "series s")),
        ("Xbox One", ("xbox one",)),
        ("Nintendo 3DS", ("nintendo 3ds", "3ds")),
        ("Nintendo DS", ("nintendo ds",)),
        ("Wii U", ("wii u",)),
        ("Wii", ("wii",)),
        ("PlayStation 3", ("playstation 3", "ps3")),
        ("Xbox 360", ("xbox 360",)),
        ("PlayStation 2", ("playstation 2", "ps2")),
        ("Xbox", ("xbox",)),
        ("Nintendo GameCube", ("gamecube", "game cube")),
    ]
    for platform, aliases in platform_aliases:
        if any(alias in lowered for alias in aliases):
            return platform
    return None
