from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from steam_mcp.cache import TtlLruCache
from steam_mcp.contracts import ErrorCode, ServiceError
from steam_mcp.cursor import CursorCodec
from steam_mcp.providers import cheapshark
from steam_mcp.providers.market_analytics import AnalyticsProviderInput, normalize_steamspy
from steam_mcp.services.game import GameService


def test_cheapshark_identifies_client(monkeypatch):
    async def get_json(url, **kwargs):
        assert url == "https://www.cheapshark.com/api/1.0/stores"
        assert "steam-research-mcp/" in kwargs["headers"]["User-Agent"]
        assert "github.com/BK927/steam-research-mcp" in kwargs["headers"]["User-Agent"]
        return []
    monkeypatch.setattr(cheapshark, "_get_json", get_json)
    assert asyncio.run(cheapshark.get_stores(cheapshark.CheapSharkStoresInput()))["stores"] == {}


@pytest.mark.parametrize(("rows", "status", "count"), [
    ([{"gameID": "9", "steamAppID": "620"}, {"gameID": "8", "steamAppID": "621"}], "available", 1),
    ([{"gameID": "9", "steamAppID": None, "external": "Portal 2"}], "unmatched", 0),
    ([{"gameID": "9", "steamAppID": "620"}, {"gameID": "8", "steamAppID": 620}], "ambiguous", 2),
])
def test_match_requires_one_exact_appid(monkeypatch, rows, status, count):
    async def request(resource, params):
        assert resource == "games" and params == {"steamAppID": 620, "limit": 60}
        return rows
    monkeypatch.setattr(cheapshark, "_request", request)
    result = asyncio.run(cheapshark.get_game_match(AnalyticsProviderInput(appid=620)))
    assert result["status"] == status
    assert result["match_count"] == count


def test_normalized_deals_are_five_distinct_stores_in_usd():
    rows = [{"storeID": str(i), "dealID": "abc%2B%2F%3D", "price": str(i), "retailPrice": "10"} for i in range(7, 0, -1)]
    rows += [{**rows[0], "price": "0"}, {**rows[1], "price": "NaN"}]
    payload = {"info": {"steamAppID": "620"}, "deals": rows, "cheapestPriceEver": {"price": "0", "date": 1234}}
    result = cheapshark.normalize_game(payload, cheapshark.CheapSharkGameInput(appid=620, game_id="9"))
    assert result["currency"] == "USD"
    assert result["available_store_count"] == 7 and result["truncated"]
    assert [row["store_id"] for row in result["deals"]] == ["7", "1", "2", "3", "4"]
    assert result["deals"][0]["price"] == "0"
    assert result["deals"][0]["discount_pct"] == 100
    assert result["deals"][0]["url"] == "https://www.cheapshark.com/redirect?dealID=abc%2B%2F%3D"
    assert result["cheapest_price_ever"] == {"price": "0", "date_unix_seconds": 1234}
    assert "not Steam-only" in result["scope"]
    payload["info"]["steamAppID"] = "621"
    with pytest.raises(ServiceError, match="did not match"):
        cheapshark.normalize_game(payload, cheapshark.CheapSharkGameInput(appid=620, game_id="9"))


class Backend:
    def __init__(self):
        self.calls = Counter()
        self.failure = None

    async def call(self, operation, arguments):
        self.calls[operation] += 1
        if operation == self.failure:
            raise ServiceError(ErrorCode.RATE_LIMITED, "limited", retryable=True)
        if operation == "steam_get_app_regional_pricing":
            return {"prices": [{"country": "kr", "currency": "KRW", "price": 11000}]}
        if operation == "steam_get_external_price_match":
            return {"status": "available", "game_id": "9", "fetched_at": "match-time"}
        if operation == "steam_get_external_stores":
            return {"stores": {"1": "Store"}}
        if operation == "steam_get_external_deals":
            return {"status": "available", "currency": "USD", "fetched_at": str(self.calls[operation]), "deals": [{"store_id": "1", "price": "1.99"}]}
        if operation == "steam_get_steamspy_analytics":
            return normalize_steamspy({"appid": 440, "average_forever": 0, "owners": "10 .. 20"})
        if operation == "steam_get_app_details":
            return {"is_free": True}
        return {"current_players": 10}


def test_optional_pricing_cache_ttls_and_partial_failure():
    async def scenario():
        now = [0.0]
        backend = Backend()
        service = GameService(backend, TtlLruCache(clock=lambda: now[0]), CursorCodec(b"c" * 32))
        async def pricing(options):
            return await service.get(620, "pricing", [], options, "", 20, {})
        default = await pricing({})
        assert default["items"][0]["currency"] == "KRW"
        assert "external_deals" not in default["data"]
        assert set(backend.calls) == {"steam_get_app_regional_pricing"}
        first = await pricing({"include_external_deals": True})
        assert first["data"]["external_deals"]["deals"][0]["store_name"] == "Store"
        now[0] = 3599
        cached = await pricing({"include_external_deals": True})
        assert cached["data"]["external_deals"]["fetched_at"] == first["data"]["external_deals"]["fetched_at"]
        now[0] = 3601
        await pricing({"include_external_deals": True})
        assert backend.calls["steam_get_external_deals"] == 2
        assert backend.calls["steam_get_external_price_match"] == backend.calls["steam_get_external_stores"] == 1
        now[0] = 7202
        backend.failure = "steam_get_external_deals"
        partial = await pricing({"include_external_deals": True})
        assert partial["items"] == default["items"]
        assert partial["data"]["external_deals"]["code"] == "RATE_LIMITED"
        assert partial["meta"]["warnings"]
        with pytest.raises(ServiceError):
            await pricing({"include_external_deals": "true"})
    asyncio.run(scenario())


def test_analytics_cached_source_times_and_zero_warning():
    async def scenario():
        backend = Backend()
        service = GameService(backend, TtlLruCache(), CursorCodec(b"c" * 32))
        async def read():
            return await service.get(440, "analytics", [], {"providers": ["steam", "steamspy"]}, "", 20, {})
        first, second = await read(), await read()
        for provider in ["steam", "steamspy"]:
            assert first["data"]["sources"][provider]["provenance"] == second["data"]["sources"][provider]["provenance"]
        assert all(count == 1 for count in backend.calls.values())
        assert first["data"]["sources"]["steam"]["store"]["is_free"]
        assert any("zero playtime" in warning for warning in first["meta"]["warnings"])
    asyncio.run(scenario())
