from __future__ import annotations

import pytest

from steam_mcp.contracts import ErrorCode, ServiceError
from steam_mcp.providers.market_analytics import (
    normalize_gamalytic,
    normalize_steamspy,
)


def test_normalize_gamalytic_free_result_keeps_provenance() -> None:
    value = normalize_gamalytic(
        {
            "cacheTimestamp": 1234,
            "result": [
                {
                    "steamId": 10,
                    "name": "Counter-Strike",
                    "copiesSold": 100,
                    "reviewScore": 97,
                    "tags": [f"tag-{index}" for index in range(30)],
                }
            ],
        },
        mode="free",
    )
    assert value["appid"] == 10
    assert value["estimated_copies_sold"] == 100
    assert value["cache_timestamp_ms"] == 1234
    assert value["provenance"]["provider"] == "gamalytic"
    assert value["provenance"]["access_mode"] == "free"
    assert value["provenance"]["kind"] == "third_party_estimate"
    assert value["provenance"]["upstream_cache_timestamp_ms"] == 1234
    assert "estimated_revenue" in value["provenance"]["missing_fields"]
    assert "estimated_copies_sold" in value["provenance"]["available_fields"]
    assert value["provenance"]["fetched_at"]
    assert len(value["tags"]) == 20


def test_normalize_steamspy_parses_range_and_review_ratio() -> None:
    value = normalize_steamspy(
        {
            "appid": 10,
            "name": "Counter-Strike",
            "owners": "10,000 .. 20,000",
            "positive": 75,
            "negative": 25,
            "ccu": 50,
            "tags": {"Action": 100, "FPS": 80},
        }
    )
    assert value["positive_review_pct"] == 75.0
    assert value["estimated_ccu"] == 50
    assert value["estimated_owners_low"] == 10_000
    assert value["estimated_owners_high"] == 20_000
    assert value["top_tags"] == {"Action": 100, "FPS": 80}
    assert value["provenance"]["kind"] == "third_party_sample_estimate"
    assert value["estimated_owners_range"] == "10,000 .. 20,000"


def test_normalizers_reject_missing_records() -> None:
    with pytest.raises(ServiceError) as gamalytic:
        normalize_gamalytic({"result": []}, mode="free")
    assert gamalytic.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(ServiceError) as steamspy:
        normalize_steamspy({"appid": 0})
    assert steamspy.value.code is ErrorCode.NOT_FOUND


def test_zero_values_are_preserved_without_inventing_missing_measurements() -> None:
    value = normalize_steamspy({"appid": 440, "average_forever": 0, "ccu": 0})
    assert value["estimated_ccu"] == 0
    assert value["average_playtime_forever_minutes"] == 0
    assert "median_playtime_forever_minutes" not in value
    assert value["provenance"]["ambiguous_zero_fields"] == ["average_playtime_forever_minutes"]
    gamalytic = normalize_gamalytic({"result": [{"steamId": 440, "copiesSold": 0, "revenue": None}]}, mode="free")
    assert gamalytic["estimated_copies_sold"] == 0
    assert "estimated_revenue" not in gamalytic
    assert "free distribution" in gamalytic["provenance"]["units"]["estimated_copies_sold"]
