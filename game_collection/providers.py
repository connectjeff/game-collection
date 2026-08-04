from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class GameMatch:
    provider: str
    provider_game_id: str
    title: str
    platform: str | None = None
    release_date: str | None = None
    developer: str | None = None
    publisher: str | None = None
    description: str | None = None
    cover_url: str | None = None
    confidence: float = 0.0
    raw: dict[str, Any] | None = None


class MetadataProvider(Protocol):
    name: str

    def search(self, title: str, platform: str | None = None, limit: int = 5) -> list[GameMatch]:
        ...


def _igdb_cover_url(image_id: str) -> str:
    return f"https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.jpg"


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "game-collection/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ProviderError(f"Metadata request failed: {exc}") from exc


def _post_json(url: str, *, data: bytes | str | None = None, headers: dict[str, str] | None = None) -> Any:
    encoded_data = data.encode("utf-8") if isinstance(data, str) else data
    request = urllib.request.Request(
        url,
        data=encoded_data,
        headers={"User-Agent": "game-collection/0.1", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ProviderError(f"Metadata request failed: {exc}") from exc


def _score_title(query: str, candidate: str) -> float:
    left = set(query.lower().replace(":", "").split())
    right = set(candidate.lower().replace(":", "").split())
    if not left or not right:
        return 0.0
    overlap = len(left & right) / len(left | right)
    exact_bonus = 0.25 if query.casefold() == candidate.casefold() else 0.0
    return min(1.0, overlap + exact_bonus)


def _first_company(game: dict[str, Any], role: str) -> str | None:
    for involved in game.get("involved_companies") or []:
        if involved.get(role):
            company = involved.get("company")
            if isinstance(company, dict) and company.get("name"):
                return str(company["name"])
    return None


def _igdb_date(timestamp: int | None) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).date().isoformat()


class TheGamesDBProvider:
    name = "thegamesdb"
    base_url = "https://api.thegamesdb.net/v1"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("THEGAMESDB_API_KEY")
        if not self.api_key:
            raise ProviderError("Set THEGAMESDB_API_KEY to use TheGamesDB.")

    def search(self, title: str, platform: str | None = None, limit: int = 5) -> list[GameMatch]:
        params = {"apikey": self.api_key, "name": title}
        if platform:
            params["filter[platform]"] = platform
        url = f"{self.base_url}/Games/ByGameName?{urllib.parse.urlencode(params)}"
        payload = _get_json(url)
        games = payload.get("data", {}).get("games") or []
        matches: list[GameMatch] = []
        for game in games[:limit]:
            game_title = game.get("game_title") or game.get("name") or ""
            matches.append(
                GameMatch(
                    provider=self.name,
                    provider_game_id=str(game.get("id")),
                    title=game_title,
                    platform=str(game.get("platform")) if game.get("platform") else platform,
                    release_date=game.get("release_date"),
                    developer=game.get("developers"),
                    publisher=game.get("publishers"),
                    description=game.get("overview"),
                    confidence=_score_title(title, game_title),
                    raw=game,
                )
            )
        return matches


class RawgProvider:
    name = "rawg"
    base_url = "https://api.rawg.io/api"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("RAWG_API_KEY")
        if not self.api_key:
            raise ProviderError("Set RAWG_API_KEY to use RAWG.")

    def search(self, title: str, platform: str | None = None, limit: int = 5) -> list[GameMatch]:
        params = {"key": self.api_key, "search": title, "page_size": str(limit)}
        url = f"{self.base_url}/games?{urllib.parse.urlencode(params)}"
        payload = _get_json(url)
        matches: list[GameMatch] = []
        for game in payload.get("results", [])[:limit]:
            game_title = game.get("name") or ""
            platforms = game.get("platforms") or []
            platform_names = [
                item.get("platform", {}).get("name")
                for item in platforms
                if item.get("platform", {}).get("name")
            ]
            platform_text = ", ".join(platform_names) or platform
            platform_bonus = 0.1 if platform and platform.lower() in platform_text.lower() else 0.0
            matches.append(
                GameMatch(
                    provider=self.name,
                    provider_game_id=str(game.get("id")),
                    title=game_title,
                    platform=platform_text,
                    release_date=game.get("released"),
                    cover_url=game.get("background_image"),
                    confidence=min(1.0, _score_title(title, game_title) + platform_bonus),
                    raw=game,
                )
            )
        return matches


class IgdbProvider:
    name = "igdb"
    base_url = "https://api.igdb.com/v4"
    token_url = "https://id.twitch.tv/oauth2/token"

    def __init__(self, client_id: str | None = None, client_secret: str | None = None) -> None:
        self.client_id = client_id or os.environ.get("IGDB_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("IGDB_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise ProviderError("Set IGDB_CLIENT_ID and IGDB_CLIENT_SECRET to use IGDB.")
        self._access_token: str | None = None

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        payload = _post_json(f"{self.token_url}?{urllib.parse.urlencode(params)}")
        token = payload.get("access_token")
        if not token:
            raise ProviderError("IGDB token response did not include an access token.")
        self._access_token = str(token)
        return self._access_token

    def _request(self, endpoint: str, body: str) -> Any:
        return _post_json(
            f"{self.base_url}/{endpoint}",
            data=body,
            headers={
                "Accept": "application/json",
                "Client-ID": self.client_id,
                "Authorization": f"Bearer {self._token()}",
            },
        )

    def search(self, title: str, platform: str | None = None, limit: int = 5) -> list[GameMatch]:
        safe_title = title.replace('"', '\\"')
        body = (
            "fields name,summary,first_release_date,cover.image_id,platforms.name,"
            "involved_companies.developer,involved_companies.publisher,involved_companies.company.name;"
            f' search "{safe_title}";'
            " where version_parent = null;"
            f" limit {limit};"
        )
        payload = self._request("games", body)
        if not isinstance(payload, list):
            raise ProviderError("IGDB search response was not a list.")

        matches: list[GameMatch] = []
        for game in payload[:limit]:
            game_title = game.get("name") or ""
            platforms = game.get("platforms") or []
            platform_names = [
                item.get("name")
                for item in platforms
                if isinstance(item, dict) and item.get("name")
            ]
            platform_text = ", ".join(platform_names) or platform
            platform_bonus = 0.1 if platform and platform.lower() in (platform_text or "").lower() else 0.0
            cover = game.get("cover") if isinstance(game.get("cover"), dict) else {}
            image_id = cover.get("image_id")
            cover_url = _igdb_cover_url(str(image_id)) if image_id else None
            matches.append(
                GameMatch(
                    provider=self.name,
                    provider_game_id=str(game.get("id")),
                    title=game_title,
                    platform=platform_text,
                    release_date=_igdb_date(game.get("first_release_date")),
                    developer=_first_company(game, "developer"),
                    publisher=_first_company(game, "publisher"),
                    description=game.get("summary"),
                    cover_url=cover_url,
                    confidence=min(1.0, _score_title(title, game_title) + platform_bonus),
                    raw=game,
                )
            )
        return matches

    def platform_ids(self, platform: str, limit: int = 10) -> list[int]:
        safe_platform = platform.replace('"', '\\"')
        payload = self._request("platforms", f'fields id,name; search "{safe_platform}"; limit {limit};')
        if not isinstance(payload, list):
            raise ProviderError("IGDB platform response was not a list.")
        exact = [item for item in payload if str(item.get("name", "")).casefold() == platform.casefold()]
        candidates = exact or payload
        return [int(item["id"]) for item in candidates if item.get("id") is not None]

    def platforms(self, limit: int = 500) -> list[dict[str, Any]]:
        platforms: list[dict[str, Any]] = []
        page_size = 500
        offset = 0
        while len(platforms) < limit:
            batch_limit = min(page_size, limit - len(platforms))
            payload = self._request(
                "platforms",
                f"fields id,name; sort name asc; limit {batch_limit}; offset {offset};",
            )
            if not isinstance(payload, list):
                raise ProviderError("IGDB platforms response was not a list.")
            if not payload:
                break
            platforms.extend(payload)
            if len(payload) < batch_limit:
                break
            offset += batch_limit
        return platforms

    def cover_index(self, *, platform: str | None = None, limit: int | None = None) -> list[GameMatch]:
        where_parts = ["cover != null", "version_parent = null"]
        if platform:
            platform_ids = self.platform_ids(platform, limit=10)
            if not platform_ids:
                raise ProviderError(f"Could not find IGDB platform: {platform}")
            if len(platform_ids) == 1:
                where_parts.append(f"platforms = {platform_ids[0]}")
            else:
                where_parts.append(f"platforms = ({','.join(str(item) for item in platform_ids)})")

        matches: list[GameMatch] = []
        seen_game_ids: set[str] = set()
        page_size = 500
        offset = 0
        while limit is None or len(matches) < limit:
            batch_limit = page_size if limit is None else min(page_size, limit - len(matches))
            body = (
                "fields name,summary,first_release_date,cover.image_id,platforms.name,"
                "involved_companies.developer,involved_companies.publisher,involved_companies.company.name;"
                f" where {' & '.join(where_parts)};"
                " sort id asc;"
                f" limit {batch_limit}; offset {offset};"
            )
            payload = self._request("games", body)
            if not isinstance(payload, list):
                raise ProviderError("IGDB cover index response was not a list.")
            if not payload:
                break
            for game in payload:
                game_id = str(game.get("id"))
                if game_id in seen_game_ids:
                    continue
                seen_game_ids.add(game_id)
                cover = game.get("cover") if isinstance(game.get("cover"), dict) else {}
                image_id = cover.get("image_id")
                if not image_id:
                    continue
                platforms = game.get("platforms") or []
                platform_names = [
                    item.get("name")
                    for item in platforms
                    if isinstance(item, dict) and item.get("name")
                ]
                matches.append(
                    GameMatch(
                        provider=self.name,
                        provider_game_id=game_id,
                        title=str(game.get("name") or ""),
                        platform=", ".join(platform_names) or platform,
                        release_date=_igdb_date(game.get("first_release_date")),
                        developer=_first_company(game, "developer"),
                        publisher=_first_company(game, "publisher"),
                        description=game.get("summary"),
                        cover_url=_igdb_cover_url(str(image_id)),
                        raw=game,
                    )
                )
            if len(payload) < batch_limit:
                break
            offset += batch_limit
        return matches


def get_provider(name: str) -> MetadataProvider:
    normalized = name.lower()
    if normalized == "thegamesdb":
        return TheGamesDBProvider()
    if normalized == "rawg":
        return RawgProvider()
    if normalized == "igdb":
        return IgdbProvider()
    raise ProviderError(f"Unknown provider: {name}")
