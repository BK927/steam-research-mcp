from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp import Client

from steam_mcp.cache import TtlLruCache
from steam_mcp.contracts import (
    ErrorCode,
    ServiceError,
    collection_envelope,
    compact_size,
    entity_envelope,
    enforce_envelope_budget,
    success_result,
)
from steam_mcp.cursor import CursorCodec
from steam_mcp.jobs import InlineJobRunner, MemoryJobStore, MemoryResultStore
from steam_mcp.public_server import (
    PUBLIC_RESOURCE_TEMPLATES,
    PUBLIC_TOOL_NAMES,
    ServerDependencies,
    create_server,
)


def run(value: Any) -> Any:
    return asyncio.run(value)


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.review_text = "review"

    async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((operation, arguments))
        if operation == "steam_search_apps":
            return {"results": [{"appid": 10, "name": "Ten"}]}
        if operation == "steam_get_player_summary":
            return {
                "count": len(arguments.get("steamids") or ["default"]),
                "players": [
                    {"steamid": value, "personaname": value}
                    for value in arguments.get("steamids") or ["default"]
                ],
            }
        if operation == "steam_get_app_review_batch":
            return {
                "reviews": [{"review": self.review_text, "voted_up": True}],
                "page": {"has_more": True, "next_cursor": "upstream-2"},
            }
        if operation == "steam_should_i_buy":
            return {"verdict": "yes", "reasons": ["good"]}
        if operation == "steam_get_app_details":
            return {"appid": arguments["appid"], "name": "Game"}
        return {"operation": operation, "arguments": arguments}


def make_server(backend: FakeBackend | None = None, *, clock: Any = None, max_result_bytes: int = 12 * 1024) -> Any:
    backend = backend or FakeBackend()
    cursor = CursorCodec(b"c" * 32, clock=clock or (lambda: 1_000.0))
    runner = InlineJobRunner()
    return create_server(
        ServerDependencies(
            backend=backend,
            cursor=cursor,
            cache=TtlLruCache(max_entries=512, ttl_seconds=600),
            job_store=MemoryJobStore(ttl_seconds=86_400),
            result_store=MemoryResultStore(),
            job_runner=runner,
            status={"job_backend": "memory", "process_role": "mcp"},
            max_result_bytes=max_result_bytes,
        )
    )


def wire(value: Any) -> dict[str, Any]:
    return value.model_dump(by_alias=True, exclude_none=False)


def call(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool(name, arguments)
            return wire(result)

    return run(go())


def resource_text(server: Any, uri: str) -> str:
    return "".join(part.content for part in run(server.read_resource(uri)))


def test_public_registry_exact_surface_and_byte_budgets() -> None:
    server = make_server()
    tools = run(server.list_tools())
    tool_rows = [
        json.dumps(
            tool.model_dump(by_alias=True, exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        for tool in tools
    ]
    assert tuple(tool.name for tool in tools) == PUBLIC_TOOL_NAMES
    # Output schemas are part of discovery now; keep their cost bounded while
    # retaining the original budget for descriptions, inputs and annotations.
    assert len(b"[" + b",".join(tool_rows) + b"]") <= 22_000
    assert all(len(row) <= 3_300 for row in tool_rows)
    input_rows = [
        json.dumps(
            {key: value for key, value in json.loads(row).items() if key != "outputSchema"},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        for row in tool_rows
    ]
    assert len(b"[" + b",".join(input_rows) + b"]") <= 6_000
    assert all(len(row) <= 1_000 for row in input_rows)
    assert all(len((tool.description or "").encode()) <= 180 for tool in tools)
    assert run(server.list_prompts()) == []
    assert run(server.list_resources()) == []
    templates = tuple(template.uri_template for template in run(server.list_resource_templates()))
    assert templates == PUBLIC_RESOURCE_TEMPLATES


def test_catalog_and_operation_schemas_are_bounded() -> None:
    server = make_server()
    catalog = resource_text(server, "steam://catalog")
    assert len(catalog.encode()) <= 8 * 1024
    assert json.loads(catalog)["tools"] == list(PUBLIC_TOOL_NAMES)
    assert json.loads(catalog)["capabilities"]["community_market"]["status"] == "experimental"
    for operation in PUBLIC_TOOL_NAMES:
        schema = resource_text(server, f"steam://schema/{operation}")
        assert len(schema.encode()) <= 4 * 1024
        assert json.loads(schema)["operation"] == operation


def test_success_and_error_wire_shapes_are_exact() -> None:
    server = make_server()
    result = call(server, "steam_search", {"query": "ten"})
    structured = result["structuredContent"]
    assert set(structured) == {"schema_version", "kind", "data", "items", "job", "page", "meta"}
    assert isinstance(structured["data"], dict)
    assert isinstance(structured["items"], list)
    assert isinstance(structured["job"], dict)
    assert set(structured["meta"]) == {
        "source",
        "provider",
        "retrieved_at",
        "fresh_until",
        "quota_cost",
        "canonical_uri",
        "warnings",
        "untrusted_fields",
    }
    assert structured["meta"]["fresh_until"] is None
    assert structured["meta"]["quota_cost"] is None
    assert len(result["content"]) == 1
    assert len(result["content"][0]["text"].encode()) <= 400

    error = call(server, "steam_search", {"mode": "lookup", "query": ""})
    assert error["isError"] is True
    assert set(error["structuredContent"]) == {
        "code",
        "message",
        "retryable",
        "schema_uri",
        "details",
    }
    assert error["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert error["structuredContent"]["schema_uri"] == "steam://schema/steam_search.lookup"


def test_strict_nested_contracts_reject_unknown_fields_before_provider_calls() -> None:
    backend = FakeBackend()
    server = make_server(backend)

    analysis = call(
        server,
        "steam_analyze",
        {"task": "game_overview", "refs": ["10"], "options": {"locale": {"country": "kr"}}},
    )["structuredContent"]
    assert analysis["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert analysis["schema_uri"] == "steam://schema/steam_analyze.game_overview"
    assert analysis["details"]["unexpected"] == ["locale"]
    assert "country" in analysis["details"]["allowed"]

    player = call(
        server,
        "steam_player_get",
        {"player": "alice", "view": "library", "options": {"foo": True}},
    )["structuredContent"]
    assert player["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert player["details"]["unexpected"] == ["foo"]

    reviews = call(
        server,
        "steam_reviews_get",
        {"game": 10, "mode": "page", "filters": {"day_range": 30}},
    )["structuredContent"]
    assert reviews["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert reviews["details"]["unexpected"] == ["day_range"]

    locale = call(
        server,
        "steam_game_get",
        {"game": 10, "locale": {"region": "KR"}},
    )["structuredContent"]
    assert locale["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert locale["schema_uri"] == "steam://schema/steam_game_get.summary"
    assert locale["details"]["unexpected"] == ["region"]
    assert backend.calls == []


def test_technical_select_error_lists_allowed_sections() -> None:
    result = call(
        make_server(),
        "steam_game_get",
        {"game": 10, "view": "technical", "select": ["appid"]},
    )["structuredContent"]
    assert result["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert result["schema_uri"] == "steam://schema/steam_game_get.technical"
    assert result["details"] == {
        "unexpected": ["appid"],
        "allowed": ["branches", "current_build", "depots", "product"],
    }


def test_player_array_contract_and_default_user_scope() -> None:
    backend = FakeBackend()
    server = make_server(backend)
    result = call(
        server,
        "steam_player_get",
        {"player": ["alice", "bob"], "view": "profile", "select": ["summary"]},
    )
    assert result["isError"] is False
    assert backend.calls[-1][1]["steamids"] == ["alice", "bob"]

    result = call(server, "steam_player_get", {"view": "profile", "select": ["summary"]})
    assert result["isError"] is False
    assert backend.calls[-1][1]["steamids"] == []

    bad_view = call(
        server,
        "steam_player_get",
        {"player": ["alice"], "view": "library"},
    )
    assert bad_view["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value
    too_many = call(
        server,
        "steam_player_get",
        {"player": [str(index) for index in range(101)], "view": "profile"},
    )
    assert too_many["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value


def test_limits_review_text_and_envelope_utf8_budgets() -> None:
    backend = FakeBackend()
    server = make_server(backend)
    default = call(server, "steam_reviews_get", {"game": 10, "mode": "page"})
    assert default["isError"] is False
    assert backend.calls[-1][1]["max_text_chars"] == 1_200
    capped = call(
        server,
        "steam_reviews_get",
        {"game": 10, "mode": "page", "max_text_chars_per_item": 50_000},
    )
    assert capped["isError"] is False
    assert backend.calls[-1][1]["max_text_chars"] == 4_000
    invalid_limit = call(server, "steam_reviews_get", {"game": 10, "limit": 101})
    assert invalid_limit["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value

    singular = success_result(entity_envelope({"text": "한" * 20_000}), "large")
    assert compact_size(singular.structured_content) <= 12 * 1024
    assert compact_size(singular.structured_content) <= 32 * 1024
    smaller = success_result(
        entity_envelope({"text": "한" * 20_000}),
        "smaller",
        max_bytes=4 * 1024,
    )
    assert compact_size(smaller.structured_content) <= 4 * 1024
    page = collection_envelope(
        [{"text": "한" * 2_000} for _ in range(20)],
        next_cursor="signed-next",
    )
    # The serializer must not reuse an upstream cursor after deleting items.
    with pytest.raises(ServiceError, match="without losing structured data"):
        success_result(page, "page")
    bounded = enforce_envelope_budget(page, continuation=lambda returned: f"after-{returned}")
    assert compact_size(bounded) <= 12 * 1024
    assert bounded["page"]["next_cursor"] == f"after-{len(bounded['items'])}"
    no_cursor = success_result(
        collection_envelope([{"text": "x" * 2_000}] * 20),
        "too large",
    ).structured_content
    assert compact_size(no_cursor) <= 12 * 1024
    assert no_cursor["page"]["has_more"] is False
    assert no_cursor["page"]["next_cursor"] is None
    assert no_cursor["meta"]["warnings"]
    truncation = no_cursor["data"]["truncation"]
    assert truncation["original_items"] == 20
    assert truncation["returned_items"] == len(no_cursor["items"])
    assert truncation["omitted_items"] == 20 - len(no_cursor["items"])


def test_runtime_reads_the_configured_result_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import steam_mcp.server as runtime

    monkeypatch.setenv("STEAM_JOB_BACKEND", "memory")
    monkeypatch.setenv("STEAM_MAX_RESULT_BYTES", "4096")
    assert runtime._public_dependencies().max_result_bytes == 4 * 1024


def test_cloud_dependency_branch_can_be_assembled(monkeypatch: pytest.MonkeyPatch) -> None:
    import steam_mcp.server as runtime

    monkeypatch.setattr(runtime, "FirestoreJobStore", lambda **_kwargs: MemoryJobStore())
    monkeypatch.setattr(runtime, "GcsResultStore", lambda _bucket: MemoryResultStore())
    monkeypatch.setattr(runtime, "CloudTasksJobRunner", lambda **_kwargs: InlineJobRunner())
    monkeypatch.setenv("STEAM_JOB_BACKEND", "gcp")
    monkeypatch.setenv("STEAM_CURSOR_SECRET", "s" * 32)
    monkeypatch.setenv("GCP_PROJECT", "example-project")
    monkeypatch.setenv("STEAM_JOB_BUCKET", "example-jobs")
    monkeypatch.setenv("STEAM_JOB_WORKER_URL", "https://worker.example")

    dependencies = runtime._public_dependencies()
    assert dependencies.status["job_backend"] == "gcp"


def test_cursor_hmac_expiry_filter_scope_and_job_expiry_override() -> None:
    now = [100.0]
    codec = CursorCodec(b"s" * 32, ttl_seconds=10, clock=lambda: now[0])
    token = codec.encode(scope="reviews", filters={"game": 10}, state={"offset": 2})
    assert codec.decode(token, scope="reviews", filters={"game": 10}) == {"offset": 2}
    failures = [
        lambda: codec.decode(token + "x", scope="reviews", filters={"game": 10}),
        lambda: codec.decode(token, scope="other", filters={"game": 10}),
        lambda: codec.decode(token, scope="reviews", filters={"game": 11}),
        lambda: codec.decode("not-a-cursor", scope="reviews", filters={"game": 10}),
    ]
    for failure in failures:
        with pytest.raises(ServiceError) as exc:
            failure()
        assert exc.value.code is ErrorCode.CURSOR_MISMATCH
    now[0] = 110.0
    with pytest.raises(ServiceError) as exc:
        codec.decode(token, scope="reviews", filters={"game": 10})
    assert exc.value.code is ErrorCode.CURSOR_MISMATCH

    job_token = codec.encode(
        scope="job:result",
        filters={"job_id": "j"},
        state={"offset": 20},
        expires_at=500.0,
    )
    now[0] = 499.0
    assert codec.decode(job_token, scope="job:result", filters={"job_id": "j"}) == {"offset": 20}
    now[0] = 500.0
    with pytest.raises(ServiceError) as exc:
        codec.decode(job_token, scope="job:result", filters={"job_id": "j"})
    assert exc.value.code is ErrorCode.CURSOR_MISMATCH


def test_cache_lru_ttl_and_inflight_coalescing() -> None:
    now = [0.0]
    cache = TtlLruCache(max_entries=512, ttl_seconds=10, clock=lambda: now[0])

    async def exercise() -> None:
        for index in range(513):
            await cache.set(str(index), index)
        assert cache.size == 512
        assert await cache.get("0") is None
        assert await cache.get("512") == 512
        now[0] = 10.0
        assert await cache.get("512") is None

        calls = 0

        async def loader() -> str:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return "loaded"

        values = await asyncio.gather(*(cache.get_or_load("same", loader) for _ in range(20)))
        assert values == ["loaded"] * 20
        assert calls == 1

    run(exercise())


def test_inline_job_finishes_before_analyze_returns_and_is_idempotent() -> None:
    backend = FakeBackend()
    server = make_server(backend)
    first = call(
        server,
        "steam_analyze",
        {"task": "purchase_decision", "refs": ["10"], "request_id": "same"},
    )
    assert first["structuredContent"]["job"]["status"] == "succeeded"
    job_id = first["structuredContent"]["job"]["job_id"]
    assert first["structuredContent"]["job"]["result_uri"] == f"steam://job/{job_id}/result/_"
    resource = json.loads(resource_text(server, f"steam://job/{job_id}/result/_"))
    assert resource["job"]["job_id"] == job_id
    assert resource["data"]["verdict"] == "yes"
    second = call(
        server,
        "steam_analyze",
        {"task": "purchase_decision", "refs": ["10"], "request_id": "same"},
    )
    assert second["structuredContent"]["job"]["job_id"] == job_id
    collision = call(
        server,
        "steam_analyze",
        {"task": "purchase_decision", "refs": ["11"], "request_id": "same"},
    )
    assert collision["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value


def test_memory_job_ttl_is_24_hours() -> None:
    now = [1_000.0]
    store = MemoryJobStore(ttl_seconds=86_400, clock=lambda: now[0])
    job = run(store.create("game_overview", ["10"], {}))
    now[0] = job.expires_at
    assert run(store.get(job.job_id)) is None


def assert_backend_calls(
    backend: FakeBackend,
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    assert [operation for operation, _ in backend.calls] == [
        operation for operation, _ in expected
    ]
    for (_, actual), (_, subset) in zip(backend.calls, expected, strict=True):
        assert {key: actual.get(key) for key in subset} == subset


@pytest.mark.parametrize(
    ("view", "extra", "expected"),
    [
        ("summary", {}, [("steam_get_app_details", {"appid": 10})]),
        ("store", {}, [("steam_get_app_details", {"appid": 10})]),
        ("compatibility", {}, [("steam_get_deck_compatibility", {"appid": 10})]),
        (
            "technical",
            {"select": ["product", "branches", "depots", "current_build"]},
            [
                ("steam_get_product_info", {"appid": 10}),
                ("steam_get_branches", {"appid": 10}),
                ("steam_get_depots", {"appid": 10}),
                ("steam_get_current_build", {"appid": 10}),
            ],
        ),
        ("dlc", {}, [("steam_get_dlc", {"appid": 10})]),
        ("tags", {}, [("steam_get_app_tags", {"appid": 10})]),
        (
            "achievements",
            {},
            [("steam_get_global_achievement_percentages", {"appid": 10})],
        ),
        ("live", {}, [("steam_get_current_players", {"appid": 10})]),
        ("news", {}, [("steam_get_app_news", {"appid": 10})]),
        ("pricing", {}, [("steam_get_app_regional_pricing", {"appid": 10})]),
        (
            "analytics",
            {"options": {"providers": ["steam"]}},
            [
                ("steam_get_app_details", {"appid": 10}),
                ("steam_get_current_players", {"appid": 10}),
                ("steam_get_app_reviews", {"appid": 10}),
            ],
        ),
    ],
)
def test_all_game_views_route_to_legacy_operations(
    view: str,
    extra: dict[str, Any],
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(make_server(backend), "steam_game_get", {"game": 10, "view": view, **extra})
    assert result["isError"] is False
    assert_backend_calls(backend, expected)


def test_analytics_provider_selection_validation_and_partial_failure() -> None:
    class PartialBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            if operation == "steam_get_steamspy_analytics":
                self.calls.append((operation, arguments))
                raise ServiceError(
                    ErrorCode.RATE_LIMITED,
                    "SteamSpy rate-limited this request.",
                    retryable=True,
                )
            return await super().call(operation, arguments)

    backend = PartialBackend()
    result = call(
        make_server(backend),
        "steam_game_get",
        {
            "game": 10,
            "view": "analytics",
            "options": {"providers": ["gamalytic", "steamspy"]},
        },
    )["structuredContent"]
    assert result["data"]["availability"]["gamalytic"]["status"] == "available"
    assert result["data"]["availability"]["steamspy"] == {
        "status": "unavailable",
        "code": ErrorCode.RATE_LIMITED.value,
        "retryable": True,
    }
    assert set(result["data"]["sources"]) == {"gamalytic"}
    assert result["meta"]["provider"] == "gamalytic"
    assert "data.sources.gamalytic.tags[]" in result["meta"]["untrusted_fields"]
    assert any("SteamSpy" in warning or "steamspy" in warning for warning in result["meta"]["warnings"])

    invalid = call(
        make_server(),
        "steam_game_get",
        {
            "game": 10,
            "view": "analytics",
            "options": {"providers": ["steam", "unknown"]},
        },
    )["structuredContent"]
    assert invalid["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert invalid["schema_uri"] == "steam://schema/steam_game_get.analytics"
    assert invalid["details"]["allowed"] == ["gamalytic", "steam", "steamspy"]


@pytest.mark.parametrize(
    ("view", "extra", "expected"),
    [
        (
            "profile",
            {},
            [
                ("steam_get_player_summary", {"steamids": ["alice"]}),
                ("steam_get_steam_level", {"steamid": "alice"}),
                ("steam_get_player_bans", {"steamid": "alice"}),
                ("steam_get_player_badges", {"steamid": "alice"}),
            ],
        ),
        (
            "social",
            {},
            [
                ("steam_get_friend_list", {"steamid": "alice"}),
                ("steam_get_user_groups", {"steamid": "alice"}),
            ],
        ),
        ("library", {}, [("steam_get_owned_games", {"steamid": "alice"})]),
        ("wishlist", {}, [("steam_get_wishlist", {"steamid": "alice"})]),
        (
            "progress",
            {"game": 10},
            [
                ("steam_get_player_achievements", {"steamid": "alice", "appid": 10}),
                ("steam_get_user_game_stats", {"steamid": "alice", "appid": 10}),
                ("steam_get_rarest_unlocks", {"steamid": "alice", "appid": 10}),
            ],
        ),
        ("inventory", {}, [("steam_get_inventory", {"steamid": "alice", "appid": 753})]),
    ],
)
def test_all_player_views_route_to_legacy_operations(
    view: str,
    extra: dict[str, Any],
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(
        make_server(backend),
        "steam_player_get",
        {"player": "alice", "view": view, **extra},
    )
    assert result["isError"] is False
    assert_backend_calls(backend, expected)


@pytest.mark.parametrize(
    ("mode", "extra", "expected"),
    [
        ("lookup", {"query": "ten"}, [("steam_search_apps", {"query": "ten"})]),
        ("discover", {}, [("steam_discover", {"term": None})]),
        ("deals", {}, [("steam_get_featured_specials", {"limit": 10})]),
        ("chart", {}, [("steam_get_store_highlights", {"section": "top_sellers"})]),
    ],
)
def test_all_search_modes_route_to_legacy_operations(
    mode: str,
    extra: dict[str, Any],
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(make_server(backend), "steam_search", {"mode": mode, **extra})
    assert result["isError"] is False
    assert_backend_calls(backend, expected)


def test_deals_apply_supported_filters_and_reject_unknown_filters() -> None:
    class DealsBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((operation, arguments))
            if operation == "steam_get_featured_specials":
                return {
                    "country": "kr",
                    "count": 3,
                    "specials": [
                        {"appid": 1, "final_price": 1, "discount_pct": 99},
                        {"appid": 2, "final_price": 63_200, "discount_pct": 20},
                        {"appid": 3, "final_price": 0.5, "discount_pct": 50},
                    ],
                }
            return await super().call(operation, arguments)

    backend = DealsBackend()
    result = call(
        make_server(backend),
        "steam_search",
        {
            "mode": "deals",
            "filters": {"max_price": 1, "min_discount": 99},
            "locale": {"country": "kr"},
        },
    )
    assert result["isError"] is False
    assert [item["appid"] for item in result["structuredContent"]["items"]] == [1]
    assert backend.calls[0][1]["limit"] == 50

    impossible = call(
        make_server(DealsBackend()),
        "steam_search",
        {
            "mode": "deals",
            "filters": {"max_price": 0, "min_discount": 100},
        },
    )
    assert impossible["structuredContent"]["kind"] == "collection"
    assert impossible["structuredContent"]["items"] == []

    unknown = call(
        make_server(),
        "steam_search",
        {
            "mode": "deals",
            "filters": {"max_price": 1, "__unknown_filter": True},
        },
    )
    assert unknown["isError"] is True
    assert unknown["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert "__unknown_filter" in unknown["structuredContent"]["message"]

    invalid = call(
        make_server(),
        "steam_search",
        {"mode": "deals", "filters": {"min_discount": 101}},
    )
    assert invalid["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("summary", [("steam_get_app_reviews", {"appid": 10, "limit": 20})]),
        (
            "page",
            [("steam_get_app_review_batch", {"appid": 10, "max_text_chars": 1_200})],
        ),
    ],
)
def test_all_review_modes_route_to_legacy_operations(
    mode: str,
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(
        make_server(backend),
        "steam_reviews_get",
        {"game": 10, "mode": mode},
    )
    assert result["isError"] is False
    assert_backend_calls(backend, expected)


@pytest.mark.parametrize(
    ("kind", "extra", "expected"),
    [
        ("package", {}, [("steam_get_package_details", {"packageid": 123})]),
        ("workshop", {}, [("steam_get_workshop_item", {"published_file_id": 123})]),
        (
            "market",
            {"options": {"appid": 730, "market_hash_name": "Item"}},
            [("steam_get_market_price", {"appid": 730, "market_hash_name": "Item"})],
        ),
    ],
)
def test_all_community_kinds_route_to_legacy_operations(
    kind: str,
    extra: dict[str, Any],
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(
        make_server(backend),
        "steam_community_get",
        {"kind": kind, "ref": "123", **extra},
    )
    assert result["isError"] is False
    assert_backend_calls(backend, expected)


def test_workshop_trust_and_market_option_validation() -> None:
    workshop = call(
        make_server(),
        "steam_community_get",
        {"kind": "workshop", "ref": "123"},
    )
    assert workshop["structuredContent"]["meta"]["untrusted_fields"] == [
        "data.title",
        "data.description",
        "data.tags[]",
    ]

    currency = call(
        make_server(),
        "steam_community_get",
        {
            "kind": "market",
            "ref": "Item",
            "options": {"appid": 730, "currency": "KRW"},
        },
    )
    assert currency["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert "integer Steam Market currency code" in currency["structuredContent"]["message"]

    unknown = call(
        make_server(),
        "steam_community_get",
        {
            "kind": "market",
            "ref": "Item",
            "options": {"appid": 730, "unknown": True},
        },
    )
    assert unknown["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value


@pytest.mark.parametrize(
    ("task", "refs", "options", "expected"),
    [
        (
            "friend_ownership",
            ["alice", "10"],
            {},
            [("steam_find_friends_who_own", {"steamid": "alice", "appid": 10})],
        ),
        (
            "review_insights",
            ["10"],
            {"max_reviews": 1},
            [("steam_get_app_review_batch", {"appid": 10, "page_size": 1})],
        ),
        (
            "game_overview",
            ["10"],
            {},
            [
                ("steam_get_app_details", {"appid": 10}),
                ("steam_get_app_tags", {"appid": 10}),
                ("steam_get_app_reviews", {"appid": 10}),
                ("steam_get_current_players", {"appid": 10}),
                ("steam_get_app_news", {"appid": 10}),
                ("steam_get_product_info", {"appid": 10}),
            ],
        ),
        (
            "player_compare",
            ["alice", "bob"],
            {},
            [("steam_compare_players", {"steamid_a": "alice", "steamid_b": "bob"})],
        ),
        (
            "library_insights",
            ["alice"],
            {},
            [("steam_analyze_library", {"steamid": "alice"})],
        ),
        (
            "purchase_decision",
            ["10"],
            {},
            [("steam_should_i_buy", {"appid": 10})],
        ),
        (
            "recommendations",
            ["10"],
            {},
            [("steam_recommend", {"seed_appid": 10})],
        ),
        (
            "coop_plan",
            ["alice", "bob"],
            {},
            [("steam_plan_coop_night", {"steamid": "alice", "friends": ["bob"]})],
        ),
    ],
)
def test_all_analysis_tasks_route_to_legacy_operations(
    task: str,
    refs: list[str],
    options: dict[str, Any],
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(
        make_server(backend),
        "steam_analyze",
        {"task": task, "refs": refs, "options": options},
    )
    assert result["isError"] is False
    assert result["structuredContent"]["job"]["status"] == "succeeded"
    assert_backend_calls(backend, expected)


def test_review_insights_reports_partial_signed_continuation_and_trust() -> None:
    backend = FakeBackend()
    server = make_server(backend)
    started = call(
        server,
        "steam_analyze",
        {
            "task": "review_insights",
            "refs": ["10"],
            "options": {"max_reviews": 1},
        },
    )
    job_id = started["structuredContent"]["job"]["job_id"]
    result = call(server, "steam_job_get", {"job_id": job_id})["structuredContent"]
    assert result["data"]["reviews_scanned"] == 1
    assert result["data"]["stop_reason"] == "max_reviews"
    assert result["data"]["partial"] is True
    assert result["data"]["corpus_complete"] is False
    assert result["data"]["complete_for_requested_scope"] is False
    continuation = result["data"]["continuation_cursor"]
    assert continuation and continuation != "upstream-2"
    assert result["meta"]["untrusted_fields"] == [
        "items[].review",
        "items[].developer_response",
    ]

    resumed = call(
        server,
        "steam_analyze",
        {
            "task": "review_insights",
            "refs": ["10"],
            "options": {"max_reviews": 1, "cursor": continuation},
        },
    )
    assert resumed["isError"] is False
    review_calls = [call for call in backend.calls if call[0] == "steam_get_app_review_batch"]
    assert review_calls[-1][1]["cursor"] == "upstream-2"


def test_review_insights_marks_actual_end_of_corpus() -> None:
    class EndBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((operation, arguments))
            if operation == "steam_get_app_review_batch":
                return {
                    "reviews": [{"review": "last", "voted_up": True}],
                    "page": {"has_more": False, "next_cursor": None},
                }
            return {"operation": operation, "arguments": arguments}

    server = make_server(EndBackend())
    started = call(
        server,
        "steam_analyze",
        {
            "task": "review_insights",
            "refs": ["10"],
            "options": {"max_reviews": 10},
        },
    )
    job_id = started["structuredContent"]["job"]["job_id"]
    data = call(server, "steam_job_get", {"job_id": job_id})["structuredContent"]["data"]
    assert data["stop_reason"] == "end_of_corpus"
    assert data["partial"] is False
    assert data["corpus_complete"] is True
    assert data["continuation_cursor"] is None


def test_achievement_limit_and_signed_cursor_page_nested_sections() -> None:
    class AchievementBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((operation, arguments))
            if operation == "steam_get_global_achievement_percentages":
                return {
                    "appid": arguments["appid"],
                    "achievements": [
                        {"api_name": "ONE", "global_pct": 90.0},
                        {"api_name": "TWO", "global_pct": 50.0},
                        {"api_name": "THREE", "global_pct": 10.0},
                    ],
                }
            return await super().call(operation, arguments)

    server = make_server(AchievementBackend())
    first = call(
        server,
        "steam_game_get",
        {"game": 10, "view": "achievements", "limit": 1},
    )["structuredContent"]
    assert [row["api_name"] for row in first["items"]] == ["ONE"]
    assert first["items"][0]["section"] == "global_rates"
    assert first["page"]["returned"] == 1
    assert first["page"]["has_more"] is True
    assert first["page"]["next_cursor"]

    second = call(
        server,
        "steam_game_get",
        {
            "game": 10,
            "view": "achievements",
            "limit": 1,
            "cursor": first["page"]["next_cursor"],
        },
    )["structuredContent"]
    assert second["items"][0]["api_name"] == "TWO"


def test_discover_uses_signed_cursor_and_snapshot_modes_are_explicit() -> None:
    class SearchBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((operation, arguments))
            if operation == "steam_discover":
                current = arguments["offset"]
                return {
                    "total_count": 81,
                    "offset": current,
                    "count": 5,
                    "has_more": current == 0,
                    "next_offset": 5 if current == 0 else None,
                    "results": [
                        {"appid": current + index + 1, "name": f"Game {current + index + 1}"}
                        for index in range(5)
                    ],
                }
            if operation == "steam_get_featured_specials":
                return {"specials": [{"appid": 1, "final_price": 10, "discount_pct": 20}]}
            return await super().call(operation, arguments)

    backend = SearchBackend()
    server = make_server(backend)
    first = call(
        server,
        "steam_search",
        {"mode": "discover", "query": "roguelike", "limit": 5},
    )["structuredContent"]
    assert first["page"]["returned"] == 5
    assert first["page"]["has_more"] is True
    assert first["page"]["next_cursor"]

    second = call(
        server,
        "steam_search",
        {
            "mode": "discover",
            "query": "roguelike",
            "limit": 5,
            "cursor": first["page"]["next_cursor"],
        },
    )["structuredContent"]
    assert second["items"][0]["appid"] == 6
    assert second["page"]["has_more"] is False
    assert [arguments["offset"] for operation, arguments in backend.calls if operation == "steam_discover"] == [0, 5]

    deals = call(server, "steam_search", {"mode": "deals", "limit": 5})[
        "structuredContent"
    ]
    assert deals["data"]["result_scope"] == "top_n_snapshot"
    assert deals["data"]["pagination_supported"] is False


def test_store_select_projection_is_strict_and_news_is_untrusted() -> None:
    class GameBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((operation, arguments))
            if operation == "steam_get_app_details":
                return {
                    "appid": arguments["appid"],
                    "name": "Game",
                    "price": "$10",
                    "genres": ["RPG"],
                }
            if operation == "steam_get_app_news":
                return {
                    "appid": arguments["appid"],
                    "news": [
                        {
                            "title": "Untrusted title",
                            "excerpt": "<a href='https://example.test'>external</a>",
                            "url": "https://example.test",
                        }
                    ],
                }
            return await super().call(operation, arguments)

    server = make_server(GameBackend())
    selected = call(
        server,
        "steam_game_get",
        {"game": 10, "view": "summary", "select": ["appid", "name"]},
    )["structuredContent"]
    assert selected["data"] == {"appid": 10, "name": "Game"}

    unsupported = call(
        server,
        "steam_game_get",
        {"game": 10, "view": "summary", "select": ["package_groups"]},
    )["structuredContent"]
    assert unsupported["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert unsupported["details"]["unsupported_select"] == ["package_groups"]

    news = call(server, "steam_game_get", {"game": 10, "view": "news"})[
        "structuredContent"
    ]
    assert news["meta"]["untrusted_fields"] == [
        "items[].title",
        "items[].excerpt",
        "items[].url",
    ]


def test_missing_store_and_package_entities_return_not_found() -> None:
    class MissingBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((operation, arguments))
            if operation == "steam_get_app_details":
                return {"message": f"No store details found for app {arguments['appid']}."}
            if operation == "steam_get_package_details":
                return {"message": f"No package details found for {arguments['packageid']}."}
            return await super().call(operation, arguments)

    server = make_server(MissingBackend())
    game = call(server, "steam_game_get", {"game": 999999, "view": "summary"})
    assert game["structuredContent"]["code"] == ErrorCode.NOT_FOUND.value
    assert game["structuredContent"]["retryable"] is False
    package = call(
        server,
        "steam_community_get",
        {"kind": "package", "ref": "999999"},
    )
    assert package["structuredContent"]["code"] == ErrorCode.NOT_FOUND.value


def test_large_job_object_has_lossless_cursor_chunks() -> None:
    class LargeOverviewBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((operation, arguments))
            if operation == "steam_get_app_details":
                return {"appid": arguments["appid"], "description": "x" * 3_000}
            if operation == "steam_get_app_news":
                return {
                    "news": [
                        {"title": "news", "excerpt": "y" * 1_000, "url": "https://example.test"}
                    ]
                }
            return {"operation": operation, "arguments": arguments}

    server = make_server(LargeOverviewBackend())
    started = call(
        server,
        "steam_analyze",
        {"task": "game_overview", "refs": ["10"]},
    )["structuredContent"]
    job_id = started["job"]["job_id"]
    cursor = ""
    chunks: list[str] = []
    for _ in range(20):
        page = call(
            server,
            "steam_job_get",
            {"job_id": job_id, "cursor": cursor, "max_chars": 500},
        )["structuredContent"]
        assert page["data"]["result_format"] == "json_text_chunks"
        assert page["page"]["returned"] == 1
        assert page["meta"]["untrusted_fields"] == ["items[].chunk"]
        chunks.append(page["items"][0]["chunk"])
        cursor = page["page"]["next_cursor"] or ""
        if not page["page"]["has_more"]:
            break
    else:  # pragma: no cover - protects against a non-advancing cursor
        raise AssertionError("job result cursor did not terminate")

    reconstructed = json.loads("".join(chunks))
    assert reconstructed["store"]["description"] == "x" * 3_000
    assert reconstructed["news"]["news"][0]["excerpt"] == "y" * 1_000


@pytest.mark.parametrize("budget", [4096, 12288, 32768])
@pytest.mark.parametrize("text", ["가" * 9000, '한🙂"\n\\' * 1800], ids=["korean", "escaped"])
def test_job_chunks_round_trip_unicode_and_json_escaping(budget: int, text: str) -> None:
    class UnicodeBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            if operation == "steam_get_app_details":
                return {"appid": 10, "description": text}
            return await super().call(operation, arguments)

    server = make_server(UnicodeBackend(), max_result_bytes=budget)
    started = call(server, "steam_analyze", {"task": "game_overview", "refs": ["10"]})["structuredContent"]
    cursor, chunks = "", []
    offset = 0
    for _ in range(100):
        response = call(server, "steam_job_get", {"job_id": started["job"]["job_id"], "cursor": cursor})
        assert response["isError"] is False
        page = response["structuredContent"]
        assert compact_size(page) <= budget
        if page["data"].get("result_format") != "json_text_chunks":
            assert page["data"]["store"]["description"] == text
            return
        assert page["page"]["returned"] == 1
        chunk = page["items"][0]["chunk"]
        assert chunk and page["data"]["chunk_start"] == offset
        offset += len(chunk)
        assert page["data"]["chunk_end"] == offset
        chunks.append(chunk)
        cursor = page["page"]["next_cursor"] or ""
        if not page["page"]["has_more"]:
            break
    else:
        pytest.fail("Job cursor did not terminate")
    assert json.loads("".join(chunks))["store"]["description"] == text


@pytest.mark.parametrize("budget", [4096, 12288])
def test_review_byte_pages_preserve_every_id_and_final_upstream_page(budget: int) -> None:
    class ReviewsBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((operation, arguments))
            assert operation == "steam_get_app_review_batch"
            first = arguments["cursor"] == "*"
            indices = range(20) if first else range(20, 25)
            return {
                "reviews": [{"id": str(i), "review": "가" * 1100, "timestamp_created": 123} for i in indices],
                "page": {"has_more": first, "next_cursor": "upstream-20" if first else None},
            }

    backend = ReviewsBackend()
    server = make_server(backend, max_result_bytes=budget)
    cursor, ids = "", []
    for _ in range(50):
        response = call(server, "steam_reviews_get", {"game": 10, "mode": "page", "cursor": cursor})
        assert response["isError"] is False
        page = response["structuredContent"]
        assert compact_size(page) <= budget
        assert page["items"]
        ids.extend(item["id"] for item in page["items"])
        cursor = page["page"]["next_cursor"] or ""
        if cursor.startswith("buffer:"):
            count = len(backend.calls)
            mismatch = call(server, "steam_reviews_get", {"game": 10, "mode": "page", "cursor": cursor, "locale": {"language": "english"}})
            assert mismatch["structuredContent"]["code"] == "CURSOR_MISMATCH"
            assert len(backend.calls) == count
            replay_args = {"game": 10, "mode": "page", "cursor": cursor}
            left = call(server, "steam_reviews_get", replay_args)["structuredContent"]
            right = call(server, "steam_reviews_get", replay_args)["structuredContent"]
            assert left["items"] == right["items"]
        if not page["page"]["has_more"]:
            break
    else:
        pytest.fail("Review cursor did not terminate")
    assert ids == [str(i) for i in range(25)]
    assert [args["cursor"] for _, args in backend.calls] == ["*", "upstream-20"]


def test_news_snapshot_and_completed_cancellation_are_explicit() -> None:
    class NewsBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            if operation == "steam_get_app_news":
                return {"news": [{"title": "News", "excerpt": "Q&amp;A <b>text</b>"}]}
            return await super().call(operation, arguments)

    server = make_server(NewsBackend())
    news = call(server, "steam_game_get", {"game": 10, "view": "news", "limit": 3})["structuredContent"]
    assert news["data"]["result_scope"] == "top_n_snapshot"
    assert news["data"]["requested_limit"] == 3
    assert news["data"]["upstream_pagination_supported"] is False
    assert news["items"][0]["excerpt"] == "Q&A text"
    technical = call(server, "steam_game_get", {"game": 10, "view": "technical"})["structuredContent"]
    assert technical["meta"]["untrusted_fields"] == ["data"]
    started = call(server, "steam_analyze", {"task": "game_overview", "refs": ["10"]})["structuredContent"]
    cancelled = call(server, "steam_job_cancel", {"job_id": started["job"]["job_id"]})
    assert cancelled["structuredContent"]["data"] == {
        "cancelled": False, "cancel_requested": False, "reason": "already_terminal",
    }
    assert "Cancelled Steam" not in cancelled["content"][0]["text"]


def test_buffered_pages_resume_from_shared_storage_and_expire() -> None:
    from steam_mcp.response_pager import ResponsePager

    now = [1000.0]
    codec = CursorCodec(b"s" * 32, ttl_seconds=30, clock=lambda: now[0])
    store = MemoryResultStore()
    first = ResponsePager(TtlLruCache(), codec, 4096, store)
    second = ResponsePager(TtlLruCache(), codec, 4096, store)
    source = collection_envelope([{"id": str(i), "text": "한" * 1000} for i in range(10)])
    loads = []

    async def load() -> dict[str, Any]:
        loads.append(True)
        return source

    page = run(first.run("test", {}, load))
    cursor = page["page"]["next_cursor"]
    ids = [item["id"] for item in page["items"]]
    for _ in range(20):
        if not page["page"]["has_more"]:
            break
        page = run(second.run("test", {"cursor": page["page"]["next_cursor"]}, load))
        assert compact_size(page) <= 4096
        ids.extend(item["id"] for item in page["items"])
    assert ids == [str(i) for i in range(10)]
    assert len(loads) == 1
    now[0] += 31
    with pytest.raises(ServiceError) as error:
        run(second.run("test", {"cursor": cursor}, load))
    assert error.value.code == ErrorCode.CURSOR_MISMATCH
