"""Request-time, keyless USD offers; never a Steam regional price history."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from ..contracts import ErrorCode, ServiceError
from .market_analytics import AnalyticsProviderInput, _get_json

BASE_URL = "https://www.cheapshark.com/api/1.0"


class CheapSharkGameInput(AnalyticsProviderInput):
    game_id: str = Field(pattern=r"^[0-9]{1,20}$")


class CheapSharkStoresInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _request(resource: str, params: dict[str, Any]) -> Any:
    return await _get_json(
        f"{BASE_URL}/{resource}", params=params,
        headers={"User-Agent": f"steam-research-mcp/{__version__} (+https://github.com/BK927/steam-research-mcp)"},
        provider="CheapShark"
    )


async def get_game_match(params: AnalyticsProviderInput) -> dict[str, Any]:
    rows = await _request("games", {"steamAppID": params.appid, "limit": 60})
    if not isinstance(rows, list):
        raise ServiceError(ErrorCode.PROVIDER_UNAVAILABLE, "CheapShark returned invalid game matches.")
    ids = sorted({
        str(row["gameID"]) for row in rows
        if isinstance(row, dict) and str(row.get("steamAppID")) == str(params.appid)
        and str(row.get("gameID", "")).isdigit() and 1 <= len(str(row["gameID"])) <= 20
    })
    return {
        "status": "available" if len(ids) == 1 else "ambiguous" if ids else "unmatched",
        "game_id": ids[0] if len(ids) == 1 else None,
        "match_count": len(ids), "fetched_at": _now(),
    }


def _money(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and 0 <= amount <= 1_000_000_000 else None
    except (InvalidOperation, ValueError):
        return None


def normalize_game(payload: Any, params: CheapSharkGameInput) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("info"), dict):
        raise ServiceError(ErrorCode.PROVIDER_UNAVAILABLE, "CheapShark returned an invalid game.")
    if str(payload["info"].get("steamAppID")) != str(params.appid):
        raise ServiceError(ErrorCode.NOT_FOUND, "CheapShark game lookup did not match the Steam App ID.")
    # Keep the cheapest offer per store; five means five stores, not five duplicate offers.
    stores: dict[str, dict[str, Any]] = {}
    rows = payload.get("deals")
    if not isinstance(rows, list):
        raise ServiceError(ErrorCode.PROVIDER_UNAVAILABLE, "CheapShark returned invalid offers.")
    for row in rows:
        if not isinstance(row, dict):
            continue
        price, retail = _money(row.get("price")), _money(row.get("retailPrice"))
        store_id, deal_id = str(row.get("storeID", "")), row.get("dealID")
        if price is None or not store_id.isdigit() or len(store_id) > 20:
            continue
        if not isinstance(deal_id, str) or not 1 <= len(deal_id) <= 1024:
            continue
        deal = {
            "store_id": store_id, "price": str(price),
            "retail_price": str(retail) if retail is not None else None,
            "discount_pct": float(round(100 * (retail - price) / retail, 2)) if retail and retail >= price else None,
            "url": f"https://www.cheapshark.com/redirect?dealID={quote(unquote(deal_id), safe='')}",
        }
        previous = stores.get(store_id)
        if previous is None or price < Decimal(previous["price"]):
            stores[store_id] = deal
    deals = sorted(stores.values(), key=lambda item: (Decimal(item["price"]), item["store_id"]))
    historical = payload.get("cheapestPriceEver")
    historical = historical if isinstance(historical, dict) else {}
    low = _money(historical.get("price"))
    date = historical.get("date")
    return {
        "provider": "cheapshark", "status": "available", "appid": params.appid,
        "game_id": params.game_id, "currency": "USD", "fetched_at": _now(),
        "scope": "CheapShark-tracked stores; not Steam-only history or regional availability",
        "deals": deals[:5], "available_store_count": len(deals), "returned": min(5, len(deals)),
        "truncated": len(deals) > 5,
        "cheapest_price_ever": {
            "price": str(low),
            "date_unix_seconds": date if isinstance(date, int) and not isinstance(date, bool) and date > 0 else None,
        } if low is not None else None,
    }


async def get_game_deals(params: CheapSharkGameInput) -> dict[str, Any]:
    return normalize_game(await _request("games", {"id": params.game_id}), params)


async def get_stores(params: CheapSharkStoresInput) -> dict[str, Any]:
    rows = await _request("stores", {})
    if not isinstance(rows, list):
        raise ServiceError(ErrorCode.PROVIDER_UNAVAILABLE, "CheapShark returned invalid stores.")
    return {
        "stores": {
            str(row["storeID"]): str(row["storeName"])[:120] for row in rows[:200]
            if isinstance(row, dict) and row.get("storeID") and row.get("storeName")
        },
        "fetched_at": _now(),
    }
