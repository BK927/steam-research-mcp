"""Exercise advertised output contracts through the public MCP client."""

import asyncio
import copy
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from mcp import Client

from steam_mcp import public_server
from steam_mcp.public_server import PUBLIC_TOOL_NAMES
from test_compact_mcp import FakeBackend, call, make_server


def test_all_public_outputs_match_their_advertised_schema() -> None:
    async def check() -> None:
        async with Client(make_server()) as client:
            tools = (await client.list_tools()).tools
            assert tuple(tool.name for tool in tools) == PUBLIC_TOOL_NAMES
            validators = {}
            for tool in tools:
                schema = tool.model_dump(by_alias=True)["outputSchema"]
                assert schema and schema["type"] == "object"
                assert set(schema["required"]) == {
                    "schema_version", "kind", "data", "items", "job", "page", "meta",
                }
                Draft202012Validator.check_schema(schema)
                validators[tool.name] = Draft202012Validator(schema)

            calls = [
                ("steam_game_get", {"game": 10}),
                ("steam_player_get", {"player": "alice"}),
                ("steam_search", {"query": "ten"}),
                ("steam_reviews_get", {"game": 10, "mode": "page"}),
                ("steam_community_get", {"kind": "package", "ref": "10"}),
                ("steam_analyze", {"task": "game_overview", "refs": ["10"]}),
            ]
            for name, arguments in calls:
                result = await client.call_tool(name, arguments)
                assert not result.is_error, result
                validators[name].validate(result.structured_content)
                assert len(result.content) == 1
                assert result.content[0].type == "text"
                assert set(result.structured_content) == {
                    "schema_version", "kind", "data", "items", "job", "page", "meta",
                }

            job_id = result.structured_content["job"]["job_id"]
            for name in ("steam_job_get", "steam_job_cancel"):
                result = await client.call_tool(name, {"job_id": job_id})
                assert not result.is_error, result
                validators[name].validate(result.structured_content)
                assert result.structured_content["job"]["job_id"] == job_id

            # Declaring a success schema must not turn existing tool errors
            # into output-validation failures or wrap the response in result.
            for name, arguments in [
                ("steam_search", {"query": ""}),
                ("steam_analyze", {"task": "game_overview", "refs": []}),
                ("steam_job_get", {"job_id": "missing"}),
                ("steam_job_cancel", {"job_id": "missing"}),
            ]:
                result = await client.call_tool(name, arguments)
                assert result.is_error
                assert set(result.structured_content) == {
                    "code", "message", "retryable", "schema_uri", "details",
                }

    asyncio.run(check())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), "2"),
        (("page", "has_more"), "false"),
        (("page", "returned"), -1),
        (("meta", "warnings"), "warning"),
        (("job", "status"), "unknown"),
        (("job", "job_id"), 123),
    ],
)
def test_output_schema_rejects_invalid_follow_up_fields(path: tuple[str, ...], value: Any) -> None:
    server = make_server()
    tool = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "steam_analyze")
    result = call(server, "steam_analyze", {"task": "game_overview", "refs": ["10"]})
    payload = copy.deepcopy(result["structuredContent"])
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    validator = Draft202012Validator(tool.output_schema)
    with pytest.raises(ValidationError):
        validator.validate(payload)
    payload = copy.deepcopy(result["structuredContent"])
    del payload["job"]
    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_server_validates_success_without_discarding_provider_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExtendedBackend(FakeBackend):
        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            if operation == "steam_get_app_details":
                return {"appid": 10, "name": "Game", "provider_extension": {"value": [1, "two", None]}}
            return await super().call(operation, arguments)

    result = call(make_server(ExtendedBackend()), "steam_game_get", {"game": 10})
    assert result["structuredContent"]["data"]["provider_extension"] == {"value": [1, "two", None]}
    assert "result" not in result["structuredContent"]

    original = public_server.success_result

    def invalid_success(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        result.structured_content["page"]["has_more"] = "false"
        return result

    monkeypatch.setattr(public_server, "success_result", invalid_success)
    result = call(make_server(), "steam_search", {"query": "ten"})
    assert result["isError"] is True
    assert not result.get("structuredContent")
