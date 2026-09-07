"""Bounded Steam review summaries and signed-cursor pages."""

from __future__ import annotations

from typing import Any

from ..contracts import ErrorCode, ServiceError
from .base import BaseService, bounded_limit, locale_values


REVIEW_FILTERS = {
    "summary": frozenset(
        {"review_filter", "day_range", "recent_max_reviews", "review_type", "purchase_type"}
    ),
    "page": frozenset(
        {"sort_by", "review_type", "purchase_type", "include_offtopic_activity", "include_author_id"}
    ),
}


class ReviewsService(BaseService):
    async def get(
        self,
        game: str | int,
        mode: str,
        filters: dict[str, Any],
        cursor: str,
        limit: int,
        max_text_chars_per_item: int,
        locale: dict[str, Any],
    ) -> dict[str, Any]:
        if mode not in {"summary", "page"}:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported review mode: {mode}.",
                schema_uri=f"steam://schema/steam_reviews_get.{mode}",
            )
        schema_uri = f"steam://schema/steam_reviews_get.{mode}"
        unexpected = sorted(set(filters) - REVIEW_FILTERS[mode])
        if unexpected:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported filters for {mode}: {', '.join(unexpected)}.",
                schema_uri=schema_uri,
                details={
                    "unexpected": unexpected,
                    "allowed": sorted(REVIEW_FILTERS[mode]),
                },
            )
        for key in ("include_offtopic_activity", "include_author_id"):
            if key in filters and not isinstance(filters[key], bool):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"filters.{key} must be boolean.",
                    schema_uri=schema_uri,
                )
        enums = {
            "review_filter": {"all", "recent"},
            "sort_by": {"recent", "updated"},
            "review_type": {"all", "positive", "negative"},
            "purchase_type": {"all", "steam", "non_steam_purchase"},
        }
        for key, allowed_values in enums.items():
            if key in filters and filters[key] not in allowed_values:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"filters.{key} is not supported.",
                    schema_uri=schema_uri,
                    details={"allowed": sorted(allowed_values)},
                )
        integer_ranges = {
            "day_range": (1, 365),
            "recent_max_reviews": (0, 50_000),
        }
        for key, (minimum, maximum) in integer_ranges.items():
            if key not in filters:
                continue
            value = filters[key]
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"filters.{key} must be an integer between {minimum} and {maximum}.",
                    schema_uri=schema_uri,
                    details={"minimum": minimum, "maximum": maximum},
                )
        language, country = locale_values(locale)
        if max_text_chars_per_item < 0:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "max_text_chars_per_item cannot be negative.")
        appid = await self.appid(game, country, language)
        size = bounded_limit(limit, 20, 100)
        text_limit = max(0, min(max_text_chars_per_item or 1_200, 4_000))
        canonical = f"steam://entity/app/{appid}"
        if mode == "summary":
            data = await self.call(
                "steam_get_app_reviews",
                {
                    "appid": appid,
                    "review_filter": filters.get("review_filter", "all"),
                    "day_range": int(filters.get("day_range", 30)),
                    "recent_max_reviews": int(filters.get("recent_max_reviews", 600)),
                    "review_type": filters.get("review_type", "all"),
                    "purchase_type": filters.get("purchase_type", "all"),
                    "limit": min(size, 20),
                    "country_code": country,
                    "language": language,
                },
                ttl=300,
            )
            # Copy cached provider rows before applying this request's smaller cap.
            if isinstance(data, dict) and isinstance(data.get("reviews"), list):
                reviews = []
                for row in data["reviews"]:
                    item = dict(row)
                    for field in ("excerpt", "review"):
                        text = item.get(field)
                        if isinstance(text, str):
                            clipped = len(text) > text_limit
                            item[field] = text[:text_limit - 1] + "…" if clipped else text
                            item[f"{field}_truncated"] = bool(item.get(f"{field}_truncated")) or clipped
                    reviews.append(item)
                data = {**data, "reviews": reviews}
            return self.result_envelope(
                data,
                canonical_uri=canonical,
                provider="steam_reviews",
                preferred_items=("reviews",),
                untrusted_fields=["items[].excerpt", "items[].review"],
            )

        cursor_filters = {
            "appid": appid,
            "filters": filters,
            "limit": size,
            "text_limit": text_limit,
            "language": language,
            "country": country,
        }
        state = self.page_state(
            cursor,
            scope="reviews:page",
            filters=cursor_filters,
            initial={"upstream_cursor": "*"},
        )
        upstream = str(state.get("upstream_cursor") or "*")
        data = await self.call(
            "steam_get_app_review_batch",
            {
                "appid": appid,
                "cursor": upstream,
                "sort_by": filters.get("sort_by", "recent"),
                "page_size": size,
                "review_type": filters.get("review_type", "all"),
                "purchase_type": filters.get("purchase_type", "all"),
                "language": language,
                "include_offtopic_activity": bool(filters.get("include_offtopic_activity", False)),
                "max_text_chars": text_limit,
                "include_author_id": bool(filters.get("include_author_id", False)),
                "country_code": country,
            },
            ttl=60,
        )
        page = data.get("page", {}) if isinstance(data, dict) else {}
        next_upstream = page.get("next_cursor")
        next_value = self.next_cursor(
            scope="reviews:page",
            filters=cursor_filters,
            state={"upstream_cursor": next_upstream} if page.get("has_more") and next_upstream else None,
        )
        return self.result_envelope(
            data,
            canonical_uri=canonical,
            provider="steam_reviews",
            preferred_items=("reviews",),
            next_cursor=next_value,
            untrusted_fields=["items[].review", "items[].developer_response"],
        )
