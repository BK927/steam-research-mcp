"""Bounded, read-only Gamalytic and SteamSpy analytics adapters."""

from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field

from ..contracts import ErrorCode, ServiceError
from ..services.base import provider_checkpoint


GAMALYTIC_GAME_URL = "https://api.gamalytic.com/game/{appid}"
GAMALYTIC_LIST_URL = "https://api.gamalytic.com/steam-games/list"
STEAMSPY_URL = "https://steamspy.com/api.php"
ALLOWED_HOSTS = frozenset({"api.gamalytic.com", "steamspy.com", "www.cheapshark.com"})
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
MAX_RETRIES = 2
TIMEOUT_SECONDS = 15.0


class AnalyticsProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    appid: int = Field(ge=1)


def _check_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ServiceError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "The analytics provider URL is not allowed.",
        )


async def _get_json(
    url: str,
    *,
    params: dict[str, Any],
    headers: dict[str, str] | None,
    provider: str,
    auth_configured: bool = False,
) -> Any:
    """Read one fixed analytics endpoint with bounded retry and no redirects."""
    _check_url(url)
    async with httpx2.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"Accept": "application/json", **(headers or {})},
    ) as client:
        for attempt in range(MAX_RETRIES + 1):
            await provider_checkpoint()
            try:
                response = await client.get(url, params=params)
            except httpx2.TimeoutException as exc:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.25 * (2**attempt) + random.uniform(0, 0.1))
                    continue
                raise ServiceError(
                    ErrorCode.TIMEOUT,
                    f"{provider} timed out.",
                    retryable=True,
                ) from exc
            except httpx2.HTTPError as exc:
                raise ServiceError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    f"{provider} could not be reached.",
                    retryable=True,
                ) from exc

            if response.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = min(float(retry_after), 2.0) if retry_after else 0.25 * (2**attempt)
                except ValueError:
                    delay = 0.25 * (2**attempt)
                await asyncio.sleep(delay + random.uniform(0, 0.1))
                continue
            if response.status_code == 429:
                raise ServiceError(
                    ErrorCode.RATE_LIMITED,
                    f"{provider} rate-limited this request.",
                    retryable=True,
                )
            if response.status_code in {401, 403} and auth_configured:
                raise ServiceError(
                    ErrorCode.AUTH_REQUIRED,
                    f"{provider} rejected the configured credentials or plan.",
                )
            if response.status_code == 404:
                raise ServiceError(ErrorCode.NOT_FOUND, f"{provider} has no record for this app.")
            if response.status_code >= 400 or 300 <= response.status_code < 400:
                raise ServiceError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    f"{provider} returned HTTP {response.status_code}.",
                    retryable=response.status_code >= 500,
                )
            await provider_checkpoint()
            try:
                return response.json()
            except (TypeError, ValueError) as exc:
                raise ServiceError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    f"{provider} returned a non-JSON response.",
                    retryable=True,
                ) from exc
    raise RuntimeError("unreachable")  # pragma: no cover


def _copy_present(source: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    return {
        target: source[key]
        for key, target in mapping.items()
        if source.get(key) is not None
    }


def _limited_strings(value: Any, maximum: int = 20) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item)[:120] for item in list(value)[:maximum] if item is not None]


def analytics_provenance(data: dict[str, Any], expected: dict[str, str]) -> None:
    """Record observation time before the normalized result enters the TTL cache."""
    provenance = data["provenance"]
    provenance["fetched_at"] = datetime.now(timezone.utc).isoformat()
    provenance["available_fields"] = sorted(set(data) - {"provenance"})
    provenance["missing_fields"] = sorted(set(expected) - set(data))
    provenance["units"] = {key: unit for key, unit in expected.items() if key in data}
    provenance["missing_value_policy"] = "Omitted means not supplied; zero is preserved, not imputed."


def normalize_gamalytic(payload: Any, *, mode: str) -> dict[str, Any]:
    if mode == "free":
        rows = payload.get("result") if isinstance(payload, dict) else None
        source = rows[0] if isinstance(rows, list) and rows else None
        cache_timestamp = payload.get("cacheTimestamp") if isinstance(payload, dict) else None
    else:
        source = payload
        cache_timestamp = None
    if not isinstance(source, dict):
        raise ServiceError(ErrorCode.NOT_FOUND, "Gamalytic has no record for this app.")

    data = _copy_present(
        source,
        {
            "steamId": "appid",
            "name": "name",
            "copiesSold": "estimated_copies_sold",
            "players": "estimated_players",
            "owners": "estimated_owners",
            "revenue": "estimated_revenue",
            "totalRevenue": "estimated_total_revenue",
            "wishlists": "estimated_wishlists",
            "followers": "followers",
            "reviews": "reviews",
            "reviewsSteam": "steam_reviews",
            "reviewScore": "review_score_pct",
            "avgPlaytime": "average_playtime",
            "releaseDate": "release_date",
            "earlyAccess": "early_access",
        },
    )
    if cache_timestamp is not None:
        data["cache_timestamp_ms"] = cache_timestamp
    genres = _limited_strings(source.get("genres"))
    tags = _limited_strings(source.get("tags"))
    if genres:
        data["genres"] = genres
    if tags:
        data["tags"] = tags
    data["provenance"] = {
        "provider": "gamalytic",
        "kind": "third_party_estimate",
        "access_mode": mode,
        "documentation": "https://api.gamalytic.com/reference/",
    }
    analytics_provenance(data, {
        "estimated_copies_sold": "copies; free distribution is not paid sales",
        "estimated_players": "players; not concurrent players",
        "estimated_owners": "owners; not sales",
        "estimated_revenue": "provider currency/unit unverified",
        "estimated_total_revenue": "provider currency/unit unverified",
        "estimated_wishlists": "wishlists", "followers": "followers",
        "reviews": "reviews", "steam_reviews": "reviews",
        "review_score_pct": "percent", "average_playtime": "provider unit unverified",
    })
    if cache_timestamp is not None:
        data["provenance"]["upstream_cache_timestamp_ms"] = cache_timestamp
    return data


async def get_gamalytic_analytics(params: AnalyticsProviderInput) -> dict[str, Any]:
    api_key = os.getenv("GAMALYTIC_API_KEY", "").strip()
    premium_fallback = False
    if api_key:
        try:
            payload = await _get_json(
                GAMALYTIC_GAME_URL.format(appid=params.appid),
                params={},
                headers={"api-key": api_key},
                provider="Gamalytic",
                auth_configured=True,
            )
            return normalize_gamalytic(payload, mode="premium")
        except ServiceError as exc:
            if exc.code is not ErrorCode.AUTH_REQUIRED:
                raise
            premium_fallback = True

    fields = ",".join(
        [
            "steamId",
            "name",
            "copiesSold",
            "players",
            "owners",
            "revenue",
            "reviews",
            "reviewScore",
            "followers",
            "avgPlaytime",
        ]
    )
    payload = await _get_json(
        GAMALYTIC_LIST_URL,
        params={"appids": params.appid, "limit": 1, "fields": fields},
        headers=None,
        provider="Gamalytic",
    )
    result = normalize_gamalytic(payload, mode="free")
    if premium_fallback:
        result["provenance"]["premium_fallback"] = "configured credential or plan was rejected"
    return result


def _integer(value: Any) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def normalize_steamspy(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload.get("appid"):
        raise ServiceError(ErrorCode.NOT_FOUND, "SteamSpy has no record for this app.")
    data = _copy_present(
        payload,
        {
            "appid": "appid",
            "name": "name",
            "developer": "developer",
            "publisher": "publisher",
            "owners": "estimated_owners_range",
            "positive": "positive_reviews",
            "negative": "negative_reviews",
            "userscore": "user_score",
            "ccu": "estimated_ccu",
            "average_forever": "average_playtime_forever_minutes",
            "average_2weeks": "average_playtime_2weeks_minutes",
            "median_forever": "median_playtime_forever_minutes",
            "median_2weeks": "median_playtime_2weeks_minutes",
            "price": "price_minor_units",
            "initialprice": "initial_price_minor_units",
            "discount": "discount_pct",
            "languages": "languages",
            "genre": "genre",
        },
    )
    positive = _integer(payload.get("positive"))
    negative = _integer(payload.get("negative"))
    if positive is not None and negative is not None and positive + negative:
        data["positive_review_pct"] = round(100 * positive / (positive + negative), 2)
    owners = str(payload.get("owners") or "")
    if ".." in owners:
        low, high = (_integer(part.strip()) for part in owners.split("..", 1))
        if low is not None and high is not None:
            data["estimated_owners_low"] = low
            data["estimated_owners_high"] = high
    tags = payload.get("tags")
    if isinstance(tags, dict):
        data["top_tags"] = dict(
            sorted(tags.items(), key=lambda item: _integer(item[1]) or 0, reverse=True)[:20]
        )
    data["provenance"] = {
        "provider": "steamspy",
        "kind": "third_party_sample_estimate",
        "documentation": "https://steamspy.com/about",
    }
    playtime_fields = (
        "average_playtime_forever_minutes", "average_playtime_2weeks_minutes",
        "median_playtime_forever_minutes", "median_playtime_2weeks_minutes",
    )
    analytics_provenance(data, {
        "estimated_owners_low": "owners; not sales",
        "estimated_owners_high": "owners; not sales",
        "estimated_ccu": "concurrent players at provider observation",
        "positive_reviews": "reviews", "negative_reviews": "reviews",
        "positive_review_pct": "percent", "discount_pct": "percent",
        "price_minor_units": "provider minor currency units; currency unverified",
        "initial_price_minor_units": "provider minor currency units; currency unverified",
        **dict.fromkeys(playtime_fields, "minutes"),
    })
    data["provenance"]["ambiguous_zero_fields"] = [
        key for key in playtime_fields if key in data and _integer(data[key]) == 0
    ]
    return data


async def get_steamspy_analytics(params: AnalyticsProviderInput) -> dict[str, Any]:
    payload = await _get_json(
        STEAMSPY_URL,
        params={"request": "appdetails", "appid": params.appid},
        headers=None,
        provider="SteamSpy",
    )
    return normalize_steamspy(payload)
