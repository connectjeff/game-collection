from __future__ import annotations

import csv
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .providers import GameMatch, IgdbProvider, MetadataProvider, ProviderError


INDEX_FIELDS = [
    "provider",
    "provider_game_id",
    "title",
    "platform",
    "release_date",
    "developer",
    "publisher",
    "description",
    "cover_url",
    "cover_path",
]

PRIORITIZED_PLATFORMS = ["PlayStation 5", "PlayStation 4", "Xbox One", "Xbox Series X|S"]
PLATFORM_CACHE_PATH = Path("review/platforms/igdb-platforms.csv")
PLATFORM_FIELDS = ["id", "name"]


@dataclass(frozen=True)
class CoverIndexEntry:
    provider: str
    provider_game_id: str
    title: str
    platform: str | None
    release_date: str | None
    developer: str | None
    publisher: str | None
    description: str | None
    cover_url: str | None
    cover_path: Path


@dataclass(frozen=True)
class PlatformCacheStatus:
    name: str
    cached: bool
    count: int


def slugify(value: str | None) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value or "all").strip("-").lower()
    return cleaned or "all"


def read_cover_index(path: Path) -> list[CoverIndexEntry]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        return [
            CoverIndexEntry(
                provider=row["provider"],
                provider_game_id=row["provider_game_id"],
                title=row["title"],
                platform=row.get("platform") or None,
                release_date=row.get("release_date") or None,
                developer=row.get("developer") or None,
                publisher=row.get("publisher") or None,
                description=row.get("description") or None,
                cover_url=row.get("cover_url") or None,
                cover_path=Path(row["cover_path"]),
            )
            for row in rows
        ]


def write_cover_index(path: Path, entries: list[CoverIndexEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {
                    "provider": entry.provider,
                    "provider_game_id": entry.provider_game_id,
                    "title": entry.title,
                    "platform": entry.platform or "",
                    "release_date": entry.release_date or "",
                    "developer": entry.developer or "",
                    "publisher": entry.publisher or "",
                    "description": entry.description or "",
                    "cover_url": entry.cover_url or "",
                    "cover_path": str(entry.cover_path),
                }
            )


def download_cover(url: str, out_path: Path) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "game-collection/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            out_path.write_bytes(response.read())
        return True
    except OSError:
        return False


def default_index_path(provider: str, platform: str | None) -> Path:
    return Path("review/cover-indexes") / provider / slugify(platform) / "index.csv"


def read_platform_cache(path: Path = PLATFORM_CACHE_PATH) -> list[str]:
    if not path.exists():
        return PRIORITIZED_PLATFORMS
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        names = [row["name"] for row in rows if row.get("name")]
    return prioritize_platforms(names)


def write_platform_cache(platforms: list[dict[str, object]], path: Path = PLATFORM_CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PLATFORM_FIELDS)
        writer.writeheader()
        for platform in platforms:
            if platform.get("id") is None or not platform.get("name"):
                continue
            writer.writerow({"id": platform["id"], "name": platform["name"]})


def prioritize_platforms(platforms: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for platform in [*PRIORITIZED_PLATFORMS, *platforms]:
        if platform and platform not in seen:
            ordered.append(platform)
            seen.add(platform)
    return ordered


def build_platform_cache(
    *,
    provider: MetadataProvider,
    cache_path: Path = PLATFORM_CACHE_PATH,
    refresh: bool = False,
    limit: int = 500,
) -> list[str]:
    if cache_path.exists() and not refresh:
        return read_platform_cache(cache_path)
    if provider.name != "igdb" or not hasattr(provider, "platforms"):
        raise ProviderError("Platform picklist caching currently supports IGDB only.")
    platforms = provider.platforms(limit=limit)
    write_platform_cache(platforms, cache_path)
    return read_platform_cache(cache_path)


def prebuild_prioritized_cover_indexes(
    *,
    provider: MetadataProvider,
    limit: int | None = None,
    refresh: bool = False,
) -> dict[str, int]:
    results: dict[str, int] = {}
    for platform in PRIORITIZED_PLATFORMS:
        entries = build_cover_index(
            provider=provider,
            platform=platform,
            index_path=default_index_path(provider.name, platform),
            limit=limit,
            refresh=refresh,
        )
        results[platform] = len(entries)
    return results


def platform_cache_statuses(provider_name: str, platforms: list[str]) -> list[PlatformCacheStatus]:
    statuses: list[PlatformCacheStatus] = []
    for platform in platforms:
        index_path = default_index_path(provider_name, platform)
        entries = read_cover_index(index_path)
        statuses.append(PlatformCacheStatus(name=platform, cached=bool(entries), count=len(entries)))
    return sorted(statuses, key=lambda item: (not item.cached, item.name.casefold()))


def build_cover_index(
    *,
    provider: MetadataProvider,
    platform: str | None,
    index_path: Path,
    limit: int | None = None,
    refresh: bool = False,
) -> list[CoverIndexEntry]:
    if index_path.exists() and not refresh:
        return read_cover_index(index_path)
    if not isinstance(provider, IgdbProvider):
        raise ProviderError("Cover-art indexing currently supports IGDB only.")

    covers_dir = index_path.parent / "covers"
    matches = provider.cover_index(platform=platform, limit=limit)

    def index_match(match: GameMatch) -> CoverIndexEntry | None:
        if not match.cover_url:
            return None
        cover_path = covers_dir / f"{match.provider_game_id}.jpg"
        if not cover_path.exists() and not download_cover(match.cover_url, cover_path):
            return None
        return CoverIndexEntry(
            provider=match.provider,
            provider_game_id=match.provider_game_id,
            title=match.title,
            platform=match.platform,
            release_date=match.release_date,
            developer=match.developer,
            publisher=match.publisher,
            description=match.description,
            cover_url=match.cover_url,
            cover_path=cover_path,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        entries = [entry for entry in executor.map(index_match, matches) if entry is not None]
    write_cover_index(index_path, entries)
    return entries
