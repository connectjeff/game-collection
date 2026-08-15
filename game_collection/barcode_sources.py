from __future__ import annotations

import csv
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .barcode_match import BarcodeCatalogEntry, normalize_barcode, write_barcode_catalog


USER_AGENT = "game-collection/0.1 (local barcode cache builder)"
WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
UPCDEV_BASE_URL = "https://upc.dev/v1"
OPEN_PRODUCTS_FACTS_BASE_URL = "https://world.openproductsfacts.org/api/v2"


class BarcodeSourceError(RuntimeError):
    pass


def download_wikidata_video_game_barcodes(out_path: Path, *, limit: int | None = None) -> list[BarcodeCatalogEntry]:
    limit_clause = f"LIMIT {limit}" if limit else ""
    query = f"""
SELECT ?item ?itemLabel ?gtin ?platformLabel WHERE {{
  ?item wdt:P3962 ?gtin.
  ?item wdt:P31 wd:Q7889.
  OPTIONAL {{ ?item wdt:P400 ?platform. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
{limit_clause}
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
    write_barcode_catalog(out_path, entries)
    return entries


def download_upcdev_products(out_path: Path, *, barcodes: list[str]) -> list[BarcodeCatalogEntry]:
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
    write_barcode_catalog(out_path, entries)
    return entries


def download_upcdev_search(out_path: Path, *, query: str) -> list[BarcodeCatalogEntry]:
    payload = _get_json(f"{UPCDEV_BASE_URL}/search?{urllib.parse.urlencode({'q': query})}")
    products = payload.get("data", {}).get("products") or []
    entries = [entry for product in products if (entry := _upcdev_product_to_entry(product))]
    write_barcode_catalog(out_path, entries)
    return entries


def download_open_products_facts_products(out_path: Path, *, barcodes: list[str]) -> list[BarcodeCatalogEntry]:
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
    write_barcode_catalog(out_path, entries)
    return entries


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
