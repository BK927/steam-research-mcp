"""Shared adapter and pagination helpers for compact public services."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, Protocol

from pydantic import BaseModel

from ..cache import TtlLruCache
from ..contracts import (
    CooperativeCancellation,
    ErrorCode,
    ServiceError,
    collection_envelope,
    entity_envelope,
)
from ..cursor import CursorCodec


_PROVIDER_CHECKPOINT: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "steam_provider_checkpoint", default=None
)


@contextmanager
def provider_checkpoint_scope(
    callback: Callable[[], Awaitable[None]],
) -> Iterator[None]:
    """Apply a cooperative job checkpoint to every nested provider request."""
    token = _PROVIDER_CHECKPOINT.set(callback)
    try:
        yield
    finally:
        _PROVIDER_CHECKPOINT.reset(token)


async def provider_checkpoint() -> None:
    callback = _PROVIDER_CHECKPOINT.get()
    if callback is not None:
        await callback()


class Backend(Protocol):
    async def call(self, operation: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class OperationBinding:
    fn: Any
    input_model: type[BaseModel]


class FunctionBackend:
    """Adapter from the old implementation functions to decorator-free services."""

    def __init__(self, operations: dict[str, OperationBinding]) -> None:
        self.operations = operations

    async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
        binding = self.operations.get(operation)
        if binding is None:
            raise ServiceError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"Backend operation {operation!r} is unavailable.",
            )
        values = dict(arguments)
        if "response_format" in binding.input_model.model_fields:
            values["response_format"] = "json"
        try:
            params = binding.input_model(**values)
        except Exception as exc:  # noqa: BLE001
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        try:
            raw = binding.fn(params)
            if inspect.isawaitable(raw):
                raw = await raw
        except (CooperativeCancellation, ServiceError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise ServiceError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "The Steam adapter failed unexpectedly.",
            ) from exc
        return decode_legacy_result(raw)


def decode_legacy_result(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if text.startswith("Error:"):
        lower = text.lower()
        if "api key" in lower or "401/403" in lower:
            code = ErrorCode.AUTH_REQUIRED
            retryable = False
        elif "429" in lower or "rate limit" in lower:
            code = ErrorCode.RATE_LIMITED
            retryable = True
        elif "timed out" in lower:
            code = ErrorCode.TIMEOUT
            retryable = True
        elif "not found" in lower or "404" in lower:
            code = ErrorCode.NOT_FOUND
            retryable = False
        else:
            code = ErrorCode.UPSTREAM_ERROR
            retryable = True
        raise ServiceError(code, text.removeprefix("Error:").strip(), retryable=retryable)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {"message": text}


class BaseService:
    def __init__(self, backend: Backend, cache: TtlLruCache, cursor: CursorCodec) -> None:
        self.backend = backend
        self.cache = cache
        self.cursor = cursor

    async def call(self, operation: str, arguments: dict[str, Any], *, ttl: int = 300) -> Any:
        raw_key = json.dumps(
            [operation, arguments], sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        key = hashlib.sha256(raw_key.encode()).hexdigest()
        return await self.cache.get_or_load(
            key,
            lambda: self.backend.call(operation, arguments),
            ttl,
        )

    async def appid(self, game: str | int, country: str = "us", language: str = "english") -> int:
        text = str(game).strip()
        if isinstance(game, bool) or not text:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "game must be a positive App ID, Steam app URL, or title.")
        match = re.fullmatch(
            r"(?:(?:https?://)?store\.steampowered\.com/|steam://entity/)?app/([^/?#]+)(?:[/?#].*)?",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            text = match.group(1)
        if isinstance(game, int) or re.fullmatch(r"[+-]?[0-9]+", text):
            appid = int(text)
            if appid <= 0:
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "Steam App IDs must be positive integers.")
            return appid
        if match or "://" in text or text.lower().startswith(("app/", "store.steampowered.com/")):
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "Expected a Steam app URL containing a positive integer App ID.")
        result = await self.call(
            "steam_search_apps",
            {"query": text, "limit": 25, "country_code": country, "language": language},
        )
        rows = result.get("results", []) if isinstance(result, dict) else []
        candidates = {
            row["appid"]: {"appid": row["appid"], "name": row.get("name", "")}
            for row in rows
            if isinstance(row, dict) and type(row.get("appid")) is int and row["appid"] > 0
        }
        if not candidates:
            raise ServiceError(ErrorCode.NOT_FOUND, f"No Steam app matched {game!r}.")

        def title_key(value: str) -> str:
            return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

        exact = [row for row in candidates.values() if title_key(str(row["name"])) == title_key(text)]
        if len(exact) == 1:
            return exact[0]["appid"]
        if len(candidates) == 1:
            return next(iter(candidates))
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "The game title is ambiguous. Choose an App ID or Steam app URL from the candidates.",
            details={"reason": "ambiguous_game", "query": text, "candidates": exact or list(candidates.values())},
        )

    def page_state(
        self,
        cursor: str,
        *,
        scope: str,
        filters: dict[str, Any],
        initial: dict[str, Any],
    ) -> dict[str, Any]:
        if not cursor:
            return dict(initial)
        return self.cursor.decode(cursor, scope=scope, filters=filters)

    def next_cursor(
        self,
        *,
        scope: str,
        filters: dict[str, Any],
        state: dict[str, Any] | None,
    ) -> str | None:
        if state is None:
            return None
        return self.cursor.encode(scope=scope, filters=filters, state=state)

    @staticmethod
    def find_items(data: Any, preferred: tuple[str, ...]) -> tuple[list[Any], dict[str, Any]]:
        if not isinstance(data, dict):
            return [], {"value": data}
        for key in preferred:
            value = data.get(key)
            if isinstance(value, list):
                extra = {k: v for k, v in data.items() if k != key and k != "page"}
                return value, extra
        return [], data

    def result_envelope(
        self,
        data: Any,
        *,
        canonical_uri: str,
        provider: str,
        preferred_items: tuple[str, ...] = (),
        next_cursor: str | None = None,
        warnings: list[str] | None = None,
        untrusted_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        items, extra = self.find_items(data, preferred_items)
        has_preferred_list = isinstance(data, dict) and any(
            isinstance(data.get(key), list) for key in preferred_items
        )
        if items or has_preferred_list or next_cursor is not None:
            return collection_envelope(
                items,
                next_cursor=next_cursor,
                canonical_uri=canonical_uri,
                provider=provider,
                warnings=warnings,
                untrusted_fields=untrusted_fields,
                extra=extra,
            )
        return entity_envelope(
            data,
            canonical_uri=canonical_uri,
            provider=provider,
            warnings=warnings,
            untrusted_fields=untrusted_fields,
        )


def bounded_limit(value: int, default: int, maximum: int) -> int:
    if value == 0:
        return default
    if not 1 <= value <= maximum:
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"limit must be between 1 and {maximum}.")
    return value


def locale_values(locale: dict[str, Any] | None) -> tuple[str, str]:
    value = locale or {}
    allowed = {"language", "country"}
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            f"Unsupported locale fields: {', '.join(unexpected)}.",
            details={"unexpected": unexpected, "allowed": sorted(allowed)},
        )
    raw_language = value.get("language", "english")
    raw_country = value.get("country", "us")
    if not isinstance(raw_language, str) or not 2 <= len(raw_language.strip()) <= 32:
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "locale.language must be a string between 2 and 32 characters.",
            details={"allowed": ["language", "country"]},
        )
    if (
        not isinstance(raw_country, str)
        or re.fullmatch(r"[A-Za-z]{2}", raw_country.strip()) is None
    ):
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "locale.country must be a two-letter country code.",
            details={"allowed": ["language", "country"]},
        )
    language = raw_language.strip()
    country = raw_country.strip().lower()
    return language, country
