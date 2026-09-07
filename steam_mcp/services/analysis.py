"""Composite Steam analyses executed through an injectable job backend."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import time
from typing import Any

from ..contracts import (
    CooperativeCancellation,
    ErrorCode,
    ServiceError,
    compact_size,
    job_envelope,
)
from ..jobs import JobRecord, JobRunner, JobStore, ResultStore, TERMINAL_STATES
from .base import BaseService, bounded_limit, provider_checkpoint_scope

ANALYSIS_TASKS = frozenset(
    {
        "friend_ownership",
        "review_insights",
        "game_overview",
        "player_compare",
        "library_insights",
        "purchase_decision",
        "recommendations",
        "coop_plan",
    }
)

ANALYSIS_OPTIONS = {
    "friend_ownership": frozenset({"max_friends", "playing_now", "limit"}),
    "review_insights": frozenset(
        {
            "sort_by", "review_type", "purchase_type", "language",
            "include_offtopic_activity", "include_author_id", "country", "cursor",
            "max_reviews", "max_pages", "max_seconds", "max_text_chars",
            "sample_per_bucket",
        }
    ),
    "game_overview": frozenset(
        {"country", "language", "news_count", "include_technical", "branch"}
    ),
    "player_compare": frozenset({"limit"}),
    "library_insights": frozenset(
        {
            "top_limit", "backlog_limit", "abandoned_limit", "abandoned_sort",
            "stale_days", "exclude_temp_clients",
        }
    ),
    "purchase_decision": frozenset(
        {"player", "country", "language", "recent_max_reviews"}
    ),
    "recommendations": frozenset(
        {"player", "tags", "max_price", "limit", "country"}
    ),
    "coop_plan": frozenset(
        {"mode", "online_only", "max_friends", "min_friends_owning", "limit", "country"}
    ),
}

ANALYSIS_INTEGER_RANGES = {
    ("friend_ownership", "max_friends"): (1, 250),
    ("friend_ownership", "limit"): (1, 100),
    ("review_insights", "max_reviews"): (1, 50_000),
    ("review_insights", "max_pages"): (0, 100_000),
    ("review_insights", "max_text_chars"): (100, 4_000),
    ("review_insights", "sample_per_bucket"): (0, 25),
    ("game_overview", "news_count"): (0, 10),
    ("player_compare", "limit"): (1, 100),
    ("library_insights", "top_limit"): (1, 50),
    ("library_insights", "backlog_limit"): (0, 100),
    ("library_insights", "abandoned_limit"): (0, 100),
    ("library_insights", "stale_days"): (30, 3_650),
    ("purchase_decision", "recent_max_reviews"): (0, 50_000),
    ("recommendations", "max_price"): (0, 1_000),
    ("recommendations", "limit"): (1, 30),
    ("coop_plan", "max_friends"): (1, 100),
    ("coop_plan", "min_friends_owning"): (1, 50),
    ("coop_plan", "limit"): (1, 50),
}

ANALYSIS_ENUMS = {
    ("review_insights", "sort_by"): frozenset({"recent", "updated"}),
    ("review_insights", "review_type"): frozenset({"all", "positive", "negative"}),
    ("review_insights", "purchase_type"): frozenset(
        {"all", "steam", "non_steam_purchase"}
    ),
    ("library_insights", "abandoned_sort"): frozenset(
        {"recent", "oldest", "playtime"}
    ),
    ("coop_plan", "mode"): frozenset({"owned", "new"}),
}

ANALYSIS_BOOLEAN_OPTIONS = frozenset(
    {
        "playing_now", "include_offtopic_activity", "include_author_id",
        "include_technical", "exclude_temp_clients", "online_only",
    }
)

REVIEW_INSIGHTS_DEFAULTS = {
    "max_reviews": 5_000,
    "max_pages": 0,
    "max_seconds": 0,
    "max_text_chars": 1_200,
    "sample_per_bucket": 4,
}


class RetryableJobError(Exception):
    pass


class AnalysisService(BaseService):
    def __init__(
        self,
        backend: Any,
        cache: Any,
        cursor: Any,
        job_store: JobStore,
        result_store: ResultStore,
        runner: JobRunner,
        *,
        max_result_bytes: int = 12 * 1024,
    ) -> None:
        super().__init__(backend, cache, cursor)
        self.job_store = job_store
        self.result_store = result_store
        self.runner = runner
        self.max_result_bytes = max_result_bytes
        bind = getattr(runner, "bind", None)
        if bind is not None:
            bind(self.run_job)

    async def start(
        self,
        task: str,
        refs: list[str],
        options: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        if task not in ANALYSIS_TASKS:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported analysis task: {task}.",
                schema_uri=f"steam://schema/steam_analyze.{task}",
            )
        if not refs:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "refs must contain at least one reference.")
        self._validate_options(task, options)
        payload = {"task": task, "refs": refs, "options": options}
        job = await self.job_store.create(task, refs, options, request_id or None)
        if job.status == "queued":
            try:
                await self.runner.submit(job.job_id, payload)
            except Exception as exc:  # noqa: BLE001
                error = {
                    "code": ErrorCode.PROVIDER_UNAVAILABLE.value,
                    "message": "The analysis job could not be queued.",
                    "retryable": True,
                    "schema_uri": None,
                    "details": {"job_id": job.job_id},
                }
                try:
                    await self.job_store.update(
                        job.job_id,
                        status="failed",
                        progress={"stage": "queue_failed", "percent": 0},
                        error=error,
                    )
                except Exception:  # noqa: BLE001
                    pass
                raise ServiceError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "The analysis job could not be queued.",
                    retryable=True,
                    details={"job_id": job.job_id},
                ) from exc
            job = await self._require_job(job.job_id)
        return job_envelope(self.public_job(job))

    async def run_job(self, job_id: str, payload: dict[str, Any]) -> None:
        job = await self._require_job(job_id)
        if job.status in TERMINAL_STATES:
            return
        if job.status == "cancel_requested":
            await self.job_store.update(job_id, status="cancelled")
            return
        attempt = max(0, int(payload.get("_attempt", 0)))
        claimed = await self.job_store.claim(job_id, attempt=attempt)
        if claimed is None:
            return
        try:
            with provider_checkpoint_scope(lambda: self._checkpoint(job_id)):
                result = await self.execute(
                    str(payload.get("task") or job.task),
                    list(payload.get("refs") or job.refs),
                    dict(payload.get("options") or job.options),
                    job_id=job_id,
                )
            await self._checkpoint(job_id)
            result_ref = await self.result_store.put(job_id, result)
            await self._checkpoint(job_id)
            await self.job_store.update(
                job_id,
                status="succeeded",
                progress={"stage": "complete", "percent": 100},
                result_ref=result_ref,
                error=None,
            )
        except CooperativeCancellation:
            await self.job_store.update(
                job_id,
                status="cancelled",
                progress={"stage": "cancelled"},
                error=None,
            )
        except ServiceError as exc:
            error = {
                "code": exc.code.value,
                "message": exc.message,
                "retryable": exc.retryable,
                "schema_uri": exc.schema_uri,
                "details": exc.details,
            }
            can_retry = bool(getattr(self.runner, "supports_retry", False)) and exc.retryable and attempt < 2
            await self.job_store.update(
                job_id,
                status="queued" if can_retry else "failed",
                progress={"stage": "retry_wait" if can_retry else "failed", "attempt": attempt + 1},
                error=error,
            )
            if can_retry:
                raise RetryableJobError(exc.message) from exc
        except Exception as exc:  # noqa: BLE001
            can_retry = bool(getattr(self.runner, "supports_retry", False)) and attempt < 2
            await self.job_store.update(
                job_id,
                status="queued" if can_retry else "failed",
                progress={"stage": "retry_wait" if can_retry else "failed", "attempt": attempt + 1},
                error={
                    "code": ErrorCode.UPSTREAM_ERROR.value,
                    "message": "The analysis worker failed unexpectedly.",
                    "retryable": can_retry,
                    "schema_uri": None,
                    "details": {},
                },
            )
            if can_retry:
                raise RetryableJobError("analysis worker retry") from exc

    async def execute(
        self,
        task: str,
        refs: list[str],
        options: dict[str, Any],
        *,
        job_id: str | None = None,
    ) -> Any:
        if job_id:
            await self._checkpoint(job_id)
        first = refs[0]
        if task == "friend_ownership":
            self._need(refs, 2, task)
            appid = await self.appid(refs[1])
            return await self.call(
                "steam_find_friends_who_own",
                {
                    "steamid": self._user(first),
                    "appid": appid,
                    "max_friends": int(options.get("max_friends", 50)),
                    "playing_now": bool(options.get("playing_now", False)),
                    "limit": int(options.get("limit", 30)),
                },
                ttl=60,
            )
        if task == "review_insights":
            appid = await self.appid(first, options.get("country", "us"), options.get("language", "english"))
            return await self._review_insights(appid, options, job_id)
        if task == "game_overview":
            appid = await self.appid(first)
            result = {}
            calls = (
                ("store", "steam_get_app_details", {"appid": appid, "country_code": options.get("country", "us"), "language": options.get("language", "english")}),
                ("tags", "steam_get_app_tags", {"appid": appid, "limit": 20, "country_code": options.get("country", "us")}),
                ("reviews", "steam_get_app_reviews", {"appid": appid, "limit": 5, "country_code": options.get("country", "us"), "language": options.get("language", "english")}),
                ("players", "steam_get_current_players", {"appid": appid}),
                ("news", "steam_get_app_news", {"appid": appid, "count": int(options.get("news_count", 3))}),
            )
            for name, operation, arguments in calls:
                if job_id:
                    await self._checkpoint(job_id)
                result[name] = await self.call(operation, arguments, ttl=120)
            if bool(options.get("include_technical", True)):
                if job_id:
                    await self._checkpoint(job_id)
                result["technical"] = await self.call(
                    "steam_get_product_info",
                    {"appid": appid, "branch": options.get("branch", "public"), "include_launch_options": False},
                    ttl=120,
                )
            return result
        if task == "player_compare":
            self._need(refs, 2, task)
            return await self.call(
                "steam_compare_players",
                {"steamid_a": self._user(first), "steamid_b": self._user(refs[1]), "limit": int(options.get("limit", 20))},
                ttl=120,
            )
        if task == "library_insights":
            return await self.call(
                "steam_analyze_library",
                {
                    "steamid": self._user(first),
                    "top_limit": int(options.get("top_limit", 10)),
                    "backlog_limit": int(options.get("backlog_limit", 100)),
                    "abandoned_limit": int(options.get("abandoned_limit", 25)),
                    "abandoned_sort": options.get("abandoned_sort", "recent"),
                    "stale_days": int(options.get("stale_days", 365)),
                    "exclude_temp_clients": bool(options.get("exclude_temp_clients", True)),
                },
                ttl=120,
            )
        if task == "purchase_decision":
            appid = await self.appid(first)
            return await self.call(
                "steam_should_i_buy",
                {
                    "appid": appid,
                    "steamid": self._user(refs[1]) if len(refs) > 1 else options.get("player"),
                    "country_code": options.get("country", "us"),
                    "language": options.get("language", "all"),
                    "recent_max_reviews": int(options.get("recent_max_reviews", 600)),
                },
                ttl=120,
            )
        if task == "recommendations":
            seed = None if options.get("tags") else await self.appid(first)
            return await self.call(
                "steam_recommend",
                {
                    "seed_appid": seed,
                    "steamid": options.get("player"),
                    "tags": options.get("tags", []),
                    "max_price": options.get("max_price"),
                    "limit": int(options.get("limit", 10)),
                    "country_code": options.get("country", "us"),
                },
                ttl=300,
            )
        if task == "coop_plan":
            return await self.call(
                "steam_plan_coop_night",
                {
                    "steamid": self._user(first),
                    "friends": [self._user(ref) for ref in refs[1:]],
                    "mode": options.get("mode", "owned"),
                    "online_only": bool(options.get("online_only", True)),
                    "max_friends": int(options.get("max_friends", 20)),
                    "min_friends_owning": int(options.get("min_friends_owning", 1)),
                    "limit": int(options.get("limit", 20)),
                    "country_code": options.get("country", "us"),
                },
                ttl=120,
            )
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"Unsupported analysis task: {task}.")

    async def _review_insights(
        self, appid: int, options: dict[str, Any], job_id: str | None
    ) -> dict[str, Any]:
        options = {**REVIEW_INSIGHTS_DEFAULTS, **options}
        cursor_filters = {
            "appid": appid,
            "sort_by": options.get("sort_by", "recent"),
            "review_type": options.get("review_type", "all"),
            "purchase_type": options.get("purchase_type", "all"),
            "language": options.get("language", "all"),
            "include_offtopic_activity": bool(
                options.get("include_offtopic_activity", False)
            ),
            "include_author_id": bool(options.get("include_author_id", False)),
            "country": options.get("country", "us"),
        }
        public_cursor = str(options.get("cursor") or "")
        if public_cursor and public_cursor != "*":
            cursor_state = self.cursor.decode(
                public_cursor,
                scope="analysis:review_insights",
                filters=cursor_filters,
            )
            cursor = str(cursor_state.get("provider_cursor") or "*")
        else:
            cursor = "*"
        max_reviews = max(1, min(int(options.get("max_reviews", 5_000)), 50_000))
        max_pages = int(options.get("max_pages", 0))
        max_seconds = float(options["max_seconds"])
        started = time.monotonic()
        scanned = positive = pages = 0
        languages: dict[str, int] = {}
        samples: list[dict[str, Any]] = []
        stop_reason = "end_of_corpus"
        while scanned < max_reviews:
            if job_id:
                await self._checkpoint(job_id)
            if max_pages and pages >= max_pages:
                stop_reason = "max_pages"
                break
            if max_seconds and time.monotonic() - started >= max_seconds:
                stop_reason = "max_seconds"
                break
            page_size = min(100, max_reviews - scanned)
            page = await self.call(
                "steam_get_app_review_batch",
                {
                    "appid": appid,
                    "cursor": cursor,
                    "sort_by": options.get("sort_by", "recent"),
                    "page_size": page_size,
                    "review_type": options.get("review_type", "all"),
                    "purchase_type": options.get("purchase_type", "all"),
                    "language": options.get("language", "all"),
                    "include_offtopic_activity": bool(options.get("include_offtopic_activity", False)),
                    "max_text_chars": min(int(options.get("max_text_chars", 1_200)), 4_000),
                    "include_author_id": bool(options.get("include_author_id", False)),
                    "country_code": options.get("country", "us"),
                },
                ttl=60,
            )
            reviews = page.get("reviews", []) if isinstance(page, dict) else []
            for review in reviews:
                scanned += 1
                positive += int(bool(review.get("voted_up")))
                language = str(review.get("language") or "unknown")
                languages[language] = languages.get(language, 0) + 1
                if len(samples) < int(options.get("sample_per_bucket", 4)) * 2:
                    samples.append(review)
            pages += 1
            page_info = page.get("page", {}) if isinstance(page, dict) else {}
            next_cursor = page_info.get("next_cursor")
            if job_id:
                await self._checkpoint(job_id)
            if not page_info.get("has_more") or not next_cursor:
                stop_reason = "end_of_corpus"
                cursor = ""
                break
            cursor = str(next_cursor)
            if scanned >= max_reviews:
                stop_reason = "max_reviews"
                break
        partial = stop_reason != "end_of_corpus"
        return {
            "appid": appid,
            "analysis_scope": {
                "method": "vote_and_language_aggregation",
                "semantic_text_analysis": False,
                "filters": {key: value for key, value in cursor_filters.items() if key != "appid"},
                "limits": {key: options[key] for key in REVIEW_INSIGHTS_DEFAULTS},
                "sample_selection": "first_reviews_in_requested_sort_order",
                "samples_retained": len(samples),
            },
            "reviews_scanned": scanned,
            "positive": positive,
            "negative": scanned - positive,
            "positive_pct": round(positive * 100 / scanned, 1) if scanned else None,
            "languages": languages,
            "pages_fetched": pages,
            "stop_reason": stop_reason,
            "partial": partial,
            "corpus_complete": not partial,
            "complete_for_requested_scope": not partial,
            "continuation_cursor": self.cursor.encode(
                scope="analysis:review_insights",
                filters=cursor_filters,
                state={"provider_cursor": cursor},
            ) if partial and cursor else None,
            "samples": samples,
        }

    async def _checkpoint(self, job_id: str) -> None:
        job = await self._require_job(job_id)
        if job.status in {"cancel_requested", "cancelled"}:
            raise CooperativeCancellation()

    async def get(self, job_id: str, cursor: str, limit: int, max_chars: int) -> dict[str, Any]:
        job = await self._require_job(job_id)
        envelope = job_envelope(self.public_job(job))
        if job.status != "succeeded" or not job.result_ref:
            return envelope
        if not 500 <= max_chars <= 32_000:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                "max_chars must be between 500 and 32000.",
                schema_uri="steam://schema/steam_job_get",
            )
        result = await self.result_store.get(job.result_ref)
        envelope["meta"]["untrusted_fields"] = self._untrusted_fields(job.task)
        page_limit = bounded_limit(limit, 20, 100)
        offset = 0
        filters = {"job_id": job_id, "limit": page_limit, "max_chars": max_chars}
        items, container = self._pageable(result)
        review_summary = None
        review_samples = False
        if job.task == "review_insights" and isinstance(result, dict):
            review_summary = {key: value for key, value in result.items() if key != "samples"}
            samples = result.get("samples", [])
            state = self.cursor.decode(cursor, scope="job:result", filters=filters) if cursor else {}
            # Keep old text cursors readable, and fall back to lossless chunks if
            # even one sample cannot fit. The aggregate stays structured in data.
            envelope["data"] = {**review_summary, "result_format": "review_samples"}
            envelope["meta"]["untrusted_fields"] = ["items[].review", "items[].developer_response"]
            sample_cursor = self.cursor.encode(
                scope="job:result", filters=filters,
                state={"mode": "list", "offset": len(samples)}, expires_at=job.expires_at,
            )
            envelope["page"] = {"returned": 1, "has_more": True, "next_cursor": sample_cursor}
            fits = True
            for sample in samples:
                envelope["items"] = [sample]
                if len(json.dumps([sample], ensure_ascii=False)) > max_chars or compact_size(envelope) > self.max_result_bytes:
                    fits = False
                    break
            if fits and state.get("mode") != "text":
                items, container = samples, envelope["data"]
                review_samples = True
            else:
                items = None
            envelope["items"] = []
            envelope["page"] = {"returned": 0, "has_more": False, "next_cursor": None}
        if items is None:
            serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            chunk_budget = min(max_chars, 8_000)
            if review_summary is None and not cursor and len(serialized) <= chunk_budget:
                envelope["data"] = result
                if compact_size(envelope) <= self.max_result_bytes:
                    return envelope
            if cursor:
                state = self.cursor.decode(cursor, scope="job:result", filters=filters)
                if state.get("mode") != "text":
                    raise ServiceError(ErrorCode.CURSOR_MISMATCH, "The job cursor mode is invalid.")
                offset = int(state.get("offset", 0))
                if offset < 0 or offset >= len(serialized):
                    raise ServiceError(ErrorCode.CURSOR_MISMATCH, "The job cursor offset is invalid.")
            envelope["meta"]["warnings"].append(
                "Job result is returned as lossless JSON text chunks; concatenate items[].chunk in cursor order before parsing."
            )
            if envelope["meta"]["untrusted_fields"]:
                envelope["meta"]["untrusted_fields"] = ["items[].chunk"]

            def set_chunk(length: int) -> None:
                chunk = serialized[offset:offset + length]
                next_offset = offset + len(chunk)
                has_more = next_offset < len(serialized)
                envelope["data"] = {
                    **(review_summary or {}),
                    "result_format": "json_text_chunks", "encoding": "utf-8",
                    "total_chars": len(serialized), "chunk_start": offset,
                    "chunk_end": next_offset, "complete": not has_more,
                }
                envelope["items"] = [{"chunk": chunk}]
                envelope["page"] = {
                    "returned": 1, "has_more": has_more,
                    "next_cursor": self.cursor.encode(
                        scope="job:result", filters=filters,
                        state={"mode": "text", "offset": next_offset}, expires_at=job.expires_at,
                    ) if has_more else None,
                }

            # Include JSON escaping, UTF-8, metadata and the signed cursor in the budget.
            high = min(chunk_budget, len(serialized) - offset)
            set_chunk(high)
            if compact_size(envelope) > self.max_result_bytes:
                low = 0
                while low < high:
                    middle = (low + high + 1) // 2
                    set_chunk(middle)
                    if compact_size(envelope) <= self.max_result_bytes:
                        low = middle
                    else:
                        high = middle - 1
                if low == 0:
                    raise ServiceError(ErrorCode.UPSTREAM_ERROR, "Job metadata exceeds the response byte budget.")
                set_chunk(low)
            return envelope
        if cursor:
            state = self.cursor.decode(cursor, scope="job:result", filters=filters)
            if state.get("mode") != "list":
                raise ServiceError(ErrorCode.CURSOR_MISMATCH, "The job cursor mode is invalid.")
            offset = int(state.get("offset", 0))
            if offset < 0 or offset >= len(items):
                raise ServiceError(ErrorCode.CURSOR_MISMATCH, "The job cursor offset is invalid.")
        page = items[offset:offset + page_limit]
        has_more = offset + len(page) < len(items)
        envelope["data"] = container
        envelope["items"] = page
        envelope["page"] = {
            "returned": len(page),
            "has_more": has_more,
            "next_cursor": self.cursor.encode(
                scope="job:result",
                filters=filters,
                state={"mode": "list", "offset": offset + len(page)},
                expires_at=job.expires_at,
            ) if has_more else None,
        }
        if review_samples:
            # Page whole samples without shortening or losing their text.
            while len(page) > 1 and (
                compact_size(envelope) > self.max_result_bytes
                or len(json.dumps(page, ensure_ascii=False)) > max_chars
            ):
                page.pop()
                envelope["page"] = {
                    "returned": len(page), "has_more": True,
                    "next_cursor": self.cursor.encode(
                        scope="job:result", filters=filters,
                        state={"mode": "list", "offset": offset + len(page)}, expires_at=job.expires_at,
                    ),
                }
        return envelope

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = await self.job_store.request_cancel(job_id)
        envelope = job_envelope(self.public_job(job))
        envelope["data"] = {
            "cancelled": job.status == "cancelled",
            "cancel_requested": job.status == "cancel_requested",
            "reason": "already_terminal" if job.status in TERMINAL_STATES else "cancellation_requested",
        }
        return envelope

    async def _require_job(self, job_id: str) -> JobRecord:
        job = await self.job_store.get(job_id)
        if job is None:
            raise ServiceError(ErrorCode.JOB_EXPIRED, "The job does not exist or has expired.")
        return job

    @staticmethod
    def public_job(job: JobRecord) -> dict[str, Any]:
        def stamp(value: float) -> str:
            return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")

        value = {
            "job_id": job.job_id,
            "task": job.task,
            "status": job.status,
            "progress": job.progress,
            "error": job.error,
            "status_uri": f"steam://job/{job.job_id}",
            "result_uri": f"steam://job/{job.job_id}/result/_" if job.result_ref else None,
            "created_at": stamp(job.created_at),
            "updated_at": stamp(job.updated_at),
            "expires_at": stamp(job.expires_at),
        }
        if job.task == "review_insights":
            value["effective_limits"] = {
                key: job.options.get(key, default) for key, default in REVIEW_INSIGHTS_DEFAULTS.items()
            }
        return value

    @staticmethod
    def _user(ref: str) -> str:
        return ref.removeprefix("steam://entity/user/")

    @staticmethod
    def _need(refs: list[str], count: int, task: str) -> None:
        if len(refs) < count:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"{task} requires {count} references.")

    @staticmethod
    def _validate_options(task: str, options: dict[str, Any]) -> None:
        allowed = ANALYSIS_OPTIONS[task]
        schema_uri = f"steam://schema/steam_analyze.{task}"
        unexpected = sorted(set(options) - allowed)
        if unexpected:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported options for {task}: {', '.join(unexpected)}.",
                schema_uri=schema_uri,
                details={"unexpected": unexpected, "allowed": sorted(allowed)},
            )
        for key in sorted(set(options) & ANALYSIS_BOOLEAN_OPTIONS):
            if not isinstance(options[key], bool):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"options.{key} must be boolean.",
                    schema_uri=schema_uri,
                )
        for (option_task, key), (minimum, maximum) in ANALYSIS_INTEGER_RANGES.items():
            if option_task != task or key not in options:
                continue
            value = options[key]
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"options.{key} must be an integer between {minimum} and {maximum}.",
                    schema_uri=schema_uri,
                    details={"minimum": minimum, "maximum": maximum},
                )
        if "max_seconds" in options:
            seconds = options["max_seconds"]
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, (int, float))
                or not math.isfinite(float(seconds))
                or not 0 <= float(seconds) <= 86_400
            ):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "options.max_seconds must be between 0 and 86400.",
                    schema_uri=schema_uri,
                )
        for (option_task, key), values in ANALYSIS_ENUMS.items():
            if option_task != task or key not in options:
                continue
            if options[key] not in values:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"options.{key} is not supported.",
                    schema_uri=schema_uri,
                    details={"allowed": sorted(values)},
                )
        if "country" in options and (
            not isinstance(options["country"], str)
            or re.fullmatch(r"[A-Za-z]{2}", options["country"].strip()) is None
        ):
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                "options.country must be a two-letter country code.",
                schema_uri=schema_uri,
            )
        for key, minimum, maximum in (
            ("language", 2, 32),
            ("branch", 1, 128),
            ("player", 1, 200),
            ("cursor", 1, 8_192),
        ):
            if key in options and (
                not isinstance(options[key], str)
                or not minimum <= len(options[key].strip()) <= maximum
            ):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"options.{key} must be a string between {minimum} and {maximum} characters.",
                    schema_uri=schema_uri,
                )
        if "tags" in options:
            tags = options["tags"]
            if (
                not isinstance(tags, list)
                or len(tags) > 10
                or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
            ):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "options.tags must contain at most 10 non-empty strings.",
                    schema_uri=schema_uri,
                )

    @staticmethod
    def _pageable(result: Any) -> tuple[list[Any] | None, Any]:
        if isinstance(result, list):
            return result, None
        if isinstance(result, dict):
            for key, value in result.items():
                if isinstance(value, list) and len(value) > 20:
                    return value, {k: v for k, v in result.items() if k != key}
        return None, result

    @staticmethod
    def _untrusted_fields(task: str) -> list[str]:
        if task == "review_insights":
            return [
                "data.samples[].review",
                "data.samples[].developer_response",
            ]
        if task == "game_overview":
            return [
                "data.reviews.reviews[].excerpt",
                "data.news.news[].title",
                "data.news.news[].excerpt",
                "data.news.news[].url",
            ]
        return []
