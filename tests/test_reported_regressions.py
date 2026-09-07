"""Regressions for incorrect game resolution and ignored review text limits."""

import json
import math
from types import SimpleNamespace
from typing import Any

import pytest

from test_compact_mcp import FakeBackend, call, make_server
from steam_mcp.legacy_backend import _fmt_review, _full_review
from steam_mcp.contracts import compact_size


@pytest.mark.parametrize("game", [-1, 0, "-1", "0", "app/0", "https://store.steampowered.com/app/0/", "https://store.steampowered.com/app/10oops/", ""])
@pytest.mark.parametrize("tool", ["steam_game_get", "steam_reviews_get"])
def test_invalid_app_ids_never_search_for_titles(game: str | int, tool: str) -> None:
    backend = FakeBackend()
    result = call(make_server(backend), tool, {"game": game})
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "INVALID_ARGUMENT"
    assert backend.calls == []


@pytest.mark.parametrize("game", [1145360, "1145360", "app/1145360", "steam://entity/app/1145360", "https://store.steampowered.com/app/1145360/Hades/?l=koreana"])
def test_explicit_valid_app_ids_bypass_title_search(game: str | int) -> None:
    backend = FakeBackend()
    result = call(make_server(backend), "steam_game_get", {"game": game})
    assert result["structuredContent"]["data"]["appid"] == 1145360
    assert all(op != "steam_search_apps" for op, _ in backend.calls)


def test_invalid_app_id_also_fails_analysis_without_provider_calls() -> None:
    backend = FakeBackend()
    result = call(make_server(backend), "steam_analyze", {"task": "review_insights", "refs": ["-1"]})["structuredContent"]
    assert result["job"]["status"] == "failed"
    assert result["job"]["error"]["code"] == "INVALID_ARGUMENT"
    assert backend.calls == []


class TitleBackend(FakeBackend):
    rows = [
        {"appid": 1145350, "name": "Hades II"},
        {"appid": 1145360, "name": "Hades"},
        {"appid": 1216200, "name": "Hades Original Soundtrack"},
    ]

    async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
        if operation == "steam_search_apps":
            self.calls.append((operation, arguments))
            return {"results": self.rows[:arguments["limit"]]}
        return await super().call(operation, arguments)


@pytest.mark.parametrize("title", ["Hades", "  hAdEs  ", "Ｈａｄｅｓ"])
def test_exact_title_beats_sequel_and_soundtrack(title: str) -> None:
    result = call(make_server(TitleBackend()), "steam_game_get", {"game": title})
    assert result["structuredContent"]["data"]["appid"] == 1145360


def test_ambiguous_title_returns_candidates_without_fetching_wrong_game() -> None:
    backend = TitleBackend()
    result = call(make_server(backend), "steam_game_get", {"game": "Had"})
    error = result["structuredContent"]
    assert result["isError"] is True
    assert error["code"] == "INVALID_ARGUMENT"
    assert error["details"]["reason"] == "ambiguous_game"
    assert {row["appid"] for row in error["details"]["candidates"]} == {1145350, 1145360, 1216200}
    assert [op for op, _ in backend.calls] == ["steam_search_apps"]


@pytest.mark.parametrize("mode,field", [("summary", "excerpt"), ("page", "review")])
def test_review_limit_includes_ellipsis_and_preserves_cached_text(mode: str, field: str) -> None:
    class ReviewsBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((operation, arguments))
            raw = {"review": "가🙂" * 99, "recommendationid": "123", "voted_up": True}
            review = _fmt_review(raw) if mode == "summary" else _full_review(raw, arguments["max_text_chars"])
            return {"reviews": [review], "page": {"has_more": False}}

    server = make_server(ReviewsBackend())
    small = call(server, "steam_reviews_get", {"game": 646570, "mode": mode, "limit": 1, "max_text_chars_per_item": 100})["structuredContent"]
    assert len(small["items"][0][field]) == 100
    assert small["items"][0][field].endswith("…")
    assert small["items"][0][f"{field}_truncated"] is True
    large = call(server, "steam_reviews_get", {"game": 646570, "mode": mode, "limit": 1, "max_text_chars_per_item": 400})["structuredContent"]
    assert len(large["items"][0][field]) == 198
    assert large["items"][0][f"{field}_truncated"] is False


def test_summary_reports_truncation_already_applied_by_provider() -> None:
    review = _fmt_review({"review": "x" * 600})
    assert len(review["excerpt"]) <= 280
    assert review["excerpt_truncated"] is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.476190477609634399", 0.476190477609634399),
        (0.5, 0.5), (" 0.5 ", 0.5), (0, 0.0), (1, 1.0),
        (None, None), ("", None), ("not a number", None),
        (True, None), (False, None), ([], None), ({}, None),
        ("NaN", None), ("Infinity", None), ("-Infinity", None),
        (float("nan"), None), (float("inf"), None), (10 ** 400, None),
    ],
)
def test_review_weighted_vote_score_is_a_finite_number_or_null(raw: Any, expected: float | None) -> None:
    review = _full_review({"weighted_vote_score": raw})
    score = review["weighted_vote_score"]
    assert score == expected
    assert score is None or (type(score) is float and math.isfinite(score))
    json.dumps(review, allow_nan=False)


def test_review_weighted_vote_score_type_is_consistent_in_public_pages_and_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    from steam_mcp import legacy_backend
    from steam_mcp.services.base import FunctionBackend, OperationBinding

    async def fake_raw(url: str, params: dict, cache_ttl: int = 0) -> dict:
        return {
            "success": 1, "cursor": "*", "query_summary": {},
            "reviews": [
                {"recommendationid": "234314124", "weighted_vote_score": "0.476190477609634399", "voted_up": True},
                {"recommendationid": "234191120", "weighted_vote_score": 0.5, "voted_up": False},
                {"recommendationid": "missing", "voted_up": True},
            ],
        }

    monkeypatch.setattr(legacy_backend, "_raw_get", fake_raw)
    backend = FunctionBackend({
        "steam_get_app_review_batch": OperationBinding(legacy_backend.steam_get_app_review_batch, legacy_backend.ReviewBatchInput),
    })
    server = make_server(backend)
    page = call(server, "steam_reviews_get", {"game": 646570, "mode": "page", "limit": 3})["structuredContent"]
    expected = [0.476190477609634399, 0.5, None]
    assert [item["weighted_vote_score"] for item in page["items"]] == expected
    job = call(server, "steam_analyze", {"task": "review_insights", "refs": ["646570"], "options": {"max_reviews": 3}})["structuredContent"]["job"]
    assert job["status"] == "succeeded"
    result = call(server, "steam_job_get", {"job_id": job["job_id"]})["structuredContent"]
    assert [item["weighted_vote_score"] for item in result["items"]] == expected


def test_negative_review_text_limit_is_rejected_before_provider_calls() -> None:
    backend = FakeBackend()
    result = call(make_server(backend), "steam_reviews_get", {"game": 646570, "max_text_chars_per_item": -1})
    assert result["structuredContent"]["code"] == "INVALID_ARGUMENT"
    assert backend.calls == []


def test_fractional_analysis_time_limit_does_not_disable_time_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from steam_mcp.services import analysis

    clock = iter([0.0, 1.0])
    monkeypatch.setattr(analysis, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    backend = FakeBackend()
    result = call(make_server(backend), "steam_analyze", {"task": "review_insights", "refs": ["10"], "options": {"max_seconds": 0.5}})["structuredContent"]
    assert result["job"]["status"] == "succeeded"
    assert backend.calls == []


@pytest.mark.parametrize("budget,max_chars", [(12288, 12000), (5000, 12000), (12288, 500)])
def test_review_analysis_keeps_summary_structured_and_samples_lossless(budget: int, max_chars: int) -> None:
    samples = [{"recommendationid": str(i), "review": "한글🙂\\\"" * 100, "voted_up": i % 2 == 0, "language": "koreana"} for i in range(8)]

    class AnalysisBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            return {"reviews": samples, "page": {"has_more": False}}

    server = make_server(AnalysisBackend(), max_result_bytes=budget)
    start = call(server, "steam_analyze", {"task": "review_insights", "refs": ["646570"]})["structuredContent"]
    assert start["job"]["effective_limits"]["max_reviews"] == 5000
    args = {"job_id": start["job"]["job_id"], "max_chars": max_chars, "limit": 3}
    cursor, chunks, actual = "", [], []
    for _ in range(100):
        response = call(server, "steam_job_get", {**args, "cursor": cursor})
        assert not response["isError"], response
        result = response["structuredContent"]
        assert compact_size(result) <= budget
        assert result["data"]["reviews_scanned"] == 8
        assert result["data"]["positive"] == 4
        assert result["data"]["corpus_complete"] is True
        assert result["data"]["stop_reason"] == "end_of_corpus"
        assert result["data"]["analysis_scope"]["semantic_text_analysis"] is False
        if result["data"]["result_format"] == "json_text_chunks":
            chunks.extend(item["chunk"] for item in result["items"])
            assert result["meta"]["untrusted_fields"] == ["items[].chunk"]
        else:
            actual.extend(result["items"])
            assert len(result["items"]) <= 3
            assert result["meta"]["untrusted_fields"] == ["items[].review", "items[].developer_response"]
        cursor = result["page"]["next_cursor"]
        if not cursor:
            break
        mismatch = call(server, "steam_job_get", {**args, "cursor": cursor, "limit": 2})
        assert mismatch["structuredContent"]["code"] == "CURSOR_MISMATCH"
    else:
        pytest.fail("sample paging did not terminate")
    if chunks:
        actual = json.loads("".join(chunks))["samples"]
    assert actual == samples
