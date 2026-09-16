"""Known-game reads translated to the existing Steam providers."""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any

from ..contracts import ErrorCode, ServiceError
from .base import BaseService, bounded_limit, locale_values

GAME_VIEWS = frozenset(
    {"summary", "store", "compatibility", "technical", "dlc", "tags", "achievements", "live", "news", "pricing", "analytics"}
)

ANALYTICS_PROVIDERS = frozenset({"steam", "gamalytic", "steamspy"})

STORE_SELECT_FIELDS = frozenset(
    {
        "appid", "name", "type", "is_free", "price", "initial_price",
        "discount_pct", "developers", "publishers", "release_date",
        "coming_soon", "genres", "categories", "features",
        "controller_support", "steam_deck", "platforms", "metacritic",
        "metacritic_url", "recommendations_total", "achievements_total",
        "dlc", "dlc_count", "required_age", "mature_content",
        "supported_languages", "full_audio_languages", "website",
        "short_description", "pc_requirements", "about_the_game",
    }
)

GAME_OPTIONS = {
    "summary": frozenset({"include_requirements", "include_long_description"}),
    "store": frozenset({"include_requirements", "include_long_description"}),
    "compatibility": frozenset(),
    "technical": frozenset(
        {"section", "branch", "platform", "include_launch_options", "include_all_manifests"}
    ),
    "dlc": frozenset({"enrich", "on_sale_only"}),
    "tags": frozenset(),
    "achievements": frozenset(),
    "live": frozenset(),
    "news": frozenset(),
    "pricing": frozenset({"countries", "include_external_deals"}),
    "analytics": frozenset({"providers"}),
}


class GameService(BaseService):
    async def get(
        self,
        game: str | int,
        view: str,
        select: list[str],
        options: dict[str, Any],
        cursor: str,
        limit: int,
        locale: dict[str, Any],
    ) -> dict[str, Any]:
        if view not in GAME_VIEWS:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported game view: {view}.",
                schema_uri=f"steam://schema/steam_game_get.{view}",
            )
        unknown_options = sorted(set(options) - GAME_OPTIONS[view])
        if unknown_options:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported options for {view}: {', '.join(unknown_options)}.",
                schema_uri=f"steam://schema/steam_game_get.{view}",
                details={
                    "unsupported_options": unknown_options,
                    "allowed": sorted(GAME_OPTIONS[view]),
                },
            )
        schema_uri = f"steam://schema/steam_game_get.{view}"
        boolean_options = {
            "include_requirements", "include_long_description",
            "include_launch_options", "include_all_manifests", "enrich",
            "on_sale_only", "include_external_deals",
        }
        invalid_bools = sorted(
            key for key in options
            if key in boolean_options and not isinstance(options[key], bool)
        )
        if invalid_bools:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Boolean options required: {', '.join(invalid_bools)}.",
                schema_uri=schema_uri,
                details={"invalid": invalid_bools},
            )
        if "section" in options and options["section"] not in {
            "product", "branches", "depots", "current_build"
        }:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                "options.section is not supported.",
                schema_uri=schema_uri,
                details={"allowed": ["branches", "current_build", "depots", "product"]},
            )
        if "platform" in options and options["platform"] not in {
            "all", "windows", "linux", "macos"
        }:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                "options.platform is not supported.",
                schema_uri=schema_uri,
                details={"allowed": ["all", "linux", "macos", "windows"]},
            )
        if "branch" in options and (
            not isinstance(options["branch"], str)
            or not 1 <= len(options["branch"].strip()) <= 128
        ):
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                "options.branch must be a string between 1 and 128 characters.",
                schema_uri=schema_uri,
            )
        if "countries" in options:
            countries = options["countries"]
            if (
                not isinstance(countries, list)
                or not 1 <= len(countries) <= 100
                or any(
                    not isinstance(country, str)
                    or re.fullmatch(r"[A-Za-z]{2}", country.strip()) is None
                    for country in countries
                )
            ):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "options.countries must contain 1 to 100 two-letter country codes.",
                    schema_uri=schema_uri,
                )
        if "providers" in options:
            providers = options["providers"]
            if (
                not isinstance(providers, list)
                or not 1 <= len(providers) <= len(ANALYTICS_PROVIDERS)
                or any(not isinstance(provider, str) for provider in providers)
                or len(set(providers)) != len(providers)
                or set(providers) - ANALYTICS_PROVIDERS
            ):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "options.providers must be a unique non-empty list of supported providers.",
                    schema_uri=schema_uri,
                    details={"allowed": sorted(ANALYTICS_PROVIDERS)},
                )
        if select and view not in {"summary", "store", "technical", "achievements"}:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"select is not supported for the {view} view.",
                schema_uri=f"steam://schema/steam_game_get.{view}",
            )
        language, country = locale_values(locale)
        appid = await self.appid(game, country, language)
        size = bounded_limit(limit, 20, 100)
        filters = {
            "appid": appid,
            "view": view,
            "select": sorted(select),
            "options": options,
            "limit": size,
            "language": language,
            "country": country,
        }
        state = self.page_state(cursor, scope=f"game:{view}", filters=filters, initial={"offset": 0})
        offset = int(state.get("offset", 0))
        preferred: tuple[str, ...] = ()
        has_more = False
        untrusted_fields: list[str] | None = None
        warnings: list[str] = []

        if view in {"summary", "store"}:
            data = await self.call(
                "steam_get_app_details",
                {
                    "appid": appid,
                    "country_code": country,
                    "language": language,
                    "include_requirements": bool(options.get("include_requirements", view == "store")),
                    "include_long_description": bool(options.get("include_long_description", False)),
                },
                ttl=600,
            )
            message = data.get("message") if isinstance(data, dict) else None
            if isinstance(message, str) and message.startswith("No store details found"):
                raise ServiceError(
                    ErrorCode.NOT_FOUND,
                    f"No accessible Steam store entity exists for app {appid}.",
                    schema_uri=f"steam://schema/steam_game_get.{view}",
                )
            if select:
                unknown_select = sorted(set(select) - STORE_SELECT_FIELDS)
                if unknown_select:
                    raise ServiceError(
                        ErrorCode.INVALID_ARGUMENT,
                        f"Unsupported select fields: {', '.join(unknown_select)}.",
                        schema_uri=f"steam://schema/steam_game_get.{view}",
                        details={
                            "unsupported_select": unknown_select,
                            "allowed": sorted(STORE_SELECT_FIELDS),
                        },
                    )
                data = {field: data[field] for field in select if field in data}
        elif view == "compatibility":
            data = await self.call(
                "steam_get_deck_compatibility", {"appid": appid, "language": language}, ttl=3_600
            )
        elif view == "technical":
            sections = select or [str(options.get("section") or "product")]
            allowed = {"product", "branches", "depots", "current_build"}
            unexpected = sorted(set(sections) - allowed)
            if unexpected:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"Unsupported technical sections: {', '.join(unexpected)}.",
                    schema_uri="steam://schema/steam_game_get.technical",
                    details={"unexpected": unexpected, "allowed": sorted(allowed)},
                )
            data = {}
            for section in sections:
                if section == "product":
                    data[section] = await self.call(
                        "steam_get_product_info",
                        {
                            "appid": appid,
                            "branch": options.get("branch", "public"),
                            "include_launch_options": bool(options.get("include_launch_options", False)),
                        },
                    )
                elif section == "branches":
                    data[section] = await self.call(
                        "steam_get_branches", {"appid": appid, "limit": size, "offset": offset}
                    )
                elif section == "depots":
                    data[section] = await self.call(
                        "steam_get_depots",
                        {
                            "appid": appid,
                            "branch": options.get("branch", "public"),
                            "platform": options.get("platform", "all"),
                            "include_all_manifests": bool(options.get("include_all_manifests", False)),
                            "limit": size,
                            "offset": offset,
                        },
                    )
                else:
                    data[section] = await self.call(
                        "steam_get_current_build",
                        {
                            "appid": appid,
                            "branch": options.get("branch", "public"),
                            "platform": options.get("platform", "all"),
                            "limit": size,
                            "offset": offset,
                        },
                    )
            has_more = any(
                isinstance(value, dict)
                and bool(value.get("has_more") or (value.get("page") or {}).get("has_more"))
                for value in data.values()
            )
        elif view == "dlc":
            data = await self.call(
                "steam_get_dlc",
                {
                    "appid": appid,
                    "limit": size,
                    "enrich": bool(options.get("enrich", True)),
                    "on_sale_only": bool(options.get("on_sale_only", False)),
                    "country_code": country,
                },
            )
            preferred = ("dlc", "items")
            has_more = bool(isinstance(data, dict) and data.get("has_more"))
        elif view == "tags":
            data = await self.call(
                "steam_get_app_tags", {"appid": appid, "limit": size, "country_code": country}
            )
            preferred = ("tags",)
        elif view == "achievements":
            # Global rates are keyless. Definitions use GetSchemaForGame and
            # remain an explicit opt-in when STEAM_API_KEY is absent.
            sections = select or ["global_rates"]
            allowed = {"definitions", "global_rates"}
            if set(sections) - allowed:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "Select definitions and/or global_rates.",
                )
            data = {}
            if "definitions" in sections:
                data["definitions"] = await self.call("steam_get_game_schema", {"appid": appid})
            if "global_rates" in sections:
                data["global_rates"] = await self.call(
                    "steam_get_global_achievement_percentages", {"appid": appid}
                )
            if not data:
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "Select definitions and/or global_rates.")
            achievement_items: list[dict[str, Any]] = []
            section_meta: dict[str, Any] = {}
            for section, value in data.items():
                achievements = value.get("achievements") if isinstance(value, dict) else None
                if isinstance(value, dict):
                    section_meta[section] = {
                        key: item for key, item in value.items() if key != "achievements"
                    }
                if isinstance(achievements, list):
                    achievement_items.extend(
                        {"section": section, **achievement}
                        for achievement in achievements
                    )
            selected = achievement_items[offset:offset + size]
            has_more = offset + len(selected) < len(achievement_items)
            data = {
                "appid": appid,
                "sections": sections,
                "section_meta": section_meta,
                "total": len(achievement_items),
                "achievements": selected,
            }
            preferred = ("achievements",)
        elif view == "live":
            data = await self.call("steam_get_current_players", {"appid": appid}, ttl=60)
        elif view == "news":
            data = await self.call("steam_get_app_news", {"appid": appid, "count": size}, ttl=300)
            data = {
                **data, "result_scope": "top_n_snapshot", "requested_limit": size,
                "upstream_pagination_supported": False,
            }
            for key in ("news", "items"):
                if isinstance(data.get(key), list):
                    data[key] = [
                        {**item, "excerpt": html.unescape(re.sub(r"<[^>]*>", "", item["excerpt"]))}
                        if isinstance(item, dict) and isinstance(item.get("excerpt"), str) else item
                        for item in data[key]
                    ]
            preferred = ("news", "items")
            untrusted_fields = ["items[].title", "items[].excerpt", "items[].url"]
        elif view == "pricing":
            data = await self.call(
                "steam_get_app_regional_pricing",
                {"appid": appid, "countries": options.get("countries") or [country]},
                ttl=300,
            )
            preferred = ("prices", "regions")
            if options.get("include_external_deals", False):
                external, warnings = await self._external_deals(appid)
                data = {**data, "external_deals": external}
                untrusted_fields = ["items", "data.external_deals.deals[].store_name", "data.external_deals.deals[].url"]
        else:
            requested = options.get("providers") or ["steam", "gamalytic", "steamspy"]
            data, warnings = await self._analytics(appid, requested, country, language)
            untrusted_fields = []
            if "gamalytic" in data["sources"]:
                untrusted_fields.extend(
                    [
                        "data.sources.gamalytic.name",
                        "data.sources.gamalytic.genres[]",
                        "data.sources.gamalytic.tags[]",
                    ]
                )
            if "steamspy" in data["sources"]:
                untrusted_fields.extend(
                    [
                        "data.sources.steamspy.name",
                        "data.sources.steamspy.developer",
                        "data.sources.steamspy.publisher",
                        "data.sources.steamspy.languages",
                        "data.sources.steamspy.genre",
                        "data.sources.steamspy.top_tags",
                    ]
                )

        next_value = self.next_cursor(
            scope=f"game:{view}",
            filters=filters,
            state={"offset": offset + size} if has_more else None,
        )
        if view == "technical":
            untrusted_fields = ["data"]
        return self.result_envelope(
            data,
            canonical_uri=f"steam://entity/app/{appid}",
            provider=(
                "+".join(data.get("sources", {}))
                if view == "analytics" and isinstance(data, dict)
                else "steamcmd" if view == "technical"
                else "steam_store+cheapshark" if view == "pricing" and data.get("external_deals", {}).get("status") == "available"
                else "steam_store"
            ),
            preferred_items=preferred,
            next_cursor=next_value,
            warnings=warnings or None,
            untrusted_fields=untrusted_fields,
        )

    async def _external_deals(self, appid: int) -> tuple[dict[str, Any], list[str]]:
        warnings: list[str] = []
        try:
            match = await self.call("steam_get_external_price_match", {"appid": appid}, ttl=86_400)
            if match["status"] != "available":
                return {"provider": "cheapshark", **match}, [
                    f"CheapShark exact App ID match is {match['status']}; no title-based substitution was made."
                ]
            deals = await self.call(
                "steam_get_external_deals", {"appid": appid, "game_id": match["game_id"]}, ttl=3_600
            )
            try:
                stores = await self.call("steam_get_external_stores", {}, ttl=86_400)
                names = stores.get("stores", {})
            except ServiceError:
                names = {}
                warnings.append("CheapShark store names unavailable; store IDs and offers retained.")
            return {
                **deals, "match_fetched_at": match.get("fetched_at"),
                "deals": [{**deal, "store_name": names.get(deal["store_id"])} for deal in deals["deals"]],
            }, warnings
        except ServiceError as exc:
            return {
                "provider": "cheapshark", "status": "unavailable",
                "code": exc.code.value, "retryable": exc.retryable,
            }, [f"CheapShark unavailable ({exc.code.value}); Steam regional prices retained."]

    async def _analytics(
        self,
        appid: int,
        requested: list[str],
        country: str,
        language: str,
    ) -> tuple[dict[str, Any], list[str]]:
        async def official() -> dict[str, Any]:
            calls = [
                self.call(
                    "steam_get_app_details",
                    {
                        "appid": appid,
                        "country_code": country,
                        "language": language,
                        "include_requirements": False,
                        "include_long_description": False,
                    },
                    ttl=600, with_freshness=True,
                ),
                self.call("steam_get_current_players", {"appid": appid}, ttl=60, with_freshness=True),
                self.call(
                    "steam_get_app_reviews",
                    {
                        "appid": appid,
                        "review_filter": "all",
                        "day_range": 30,
                        "recent_max_reviews": 0,
                        "review_type": "all",
                        "purchase_type": "steam",
                        "limit": 0,
                        "country_code": country,
                        "language": "all",
                    },
                    ttl=300, with_freshness=True,
                ),
            ]
            rows = await asyncio.gather(*calls, return_exceptions=True)
            names = ("store", "live", "reviews")
            available = {
                name: row for name, row in zip(names, rows, strict=True)
                if not isinstance(row, Exception)
            }
            if not available:
                first = rows[0]
                if isinstance(first, ServiceError):
                    raise first
                raise ServiceError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "Official Steam analytics are unavailable.",
                    retryable=True,
                )
            unavailable = [
                name for name, row in zip(names, rows, strict=True)
                if isinstance(row, Exception)
            ]
            return {
                **available,
                "unavailable_components": unavailable,
                "provenance": {
                    "provider": "steam",
                    "kind": "official_first_party",
                    "documentation": "https://partner.steamgames.com/doc/webapi",
                    "available_fields": sorted(available),
                    "component_fetched_at": {
                        name: row.get("fetched_at") for name, row in available.items() if isinstance(row, dict)
                    },
                    "units": {"live.current_players": "currently concurrent players; not owners or sales"},
                },
            }

        async def load(provider: str) -> dict[str, Any]:
            if provider == "steam":
                return await official()
            operation = {
                "gamalytic": "steam_get_gamalytic_analytics",
                "steamspy": "steam_get_steamspy_analytics",
            }[provider]
            return await self.call(operation, {"appid": appid}, ttl=86_400)

        rows = await asyncio.gather(
            *(load(provider) for provider in requested),
            return_exceptions=True,
        )
        sources: dict[str, Any] = {}
        availability: dict[str, Any] = {}
        warnings: list[str] = []
        for provider, row in zip(requested, rows, strict=True):
            if isinstance(row, Exception):
                code = row.code.value if isinstance(row, ServiceError) else ErrorCode.PROVIDER_UNAVAILABLE.value
                retryable = row.retryable if isinstance(row, ServiceError) else True
                availability[provider] = {
                    "status": "unavailable",
                    "code": code,
                    "retryable": retryable,
                }
                warnings.append(f"{provider} was unavailable ({code}); other sources were retained.")
                continue
            sources[provider] = row
            availability[provider] = {"status": "available"}

        if not sources:
            raise ServiceError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "None of the requested analytics providers were available.",
                retryable=any(value.get("retryable") for value in availability.values()),
                schema_uri="steam://schema/steam_game_get.analytics",
                details={"providers": availability},
            )
        if "gamalytic" in sources:
            warnings.append("Gamalytic sales, player, owner and revenue values are third-party estimates.")
            warnings.append("Gamalytic copiesSold for free-to-play games must not be interpreted as paid sales. Missing fields were not supplied; Steam API keys do not unlock Gamalytic plans.")
            if sources["gamalytic"].get("provenance", {}).get("access_mode") == "free":
                warnings.append("Gamalytic is using its keyless public field subset.")
        if "steamspy" in sources:
            warnings.append(
                "SteamSpy values are sample-based estimates; owners are not sales and recent releases may be unreliable."
            )
            if sources["steamspy"].get("provenance", {}).get("ambiguous_zero_fields"):
                warnings.append("SteamSpy reported zero playtime values; their meaning is uncertain. Zeros were preserved and do not establish zero player activity.")

        comparison: dict[str, Any] = {}
        steam = sources.get("steam", {})
        if isinstance(steam.get("live"), dict):
            comparison["official_current_players"] = steam["live"].get("current_players")
        summary = steam.get("reviews", {}).get("official_store_summary") if isinstance(steam.get("reviews"), dict) else None
        if isinstance(summary, dict):
            comparison["official_review_positive_pct"] = summary.get("positive_pct")
            comparison["official_review_total_positive"] = summary.get("total_positive")
            comparison["official_review_total_negative"] = summary.get("total_negative")
        gamalytic = sources.get("gamalytic", {})
        for key in ("estimated_copies_sold", "estimated_players", "estimated_owners", "estimated_revenue"):
            if key in gamalytic:
                comparison[f"gamalytic_{key}"] = gamalytic[key]
        steamspy = sources.get("steamspy", {})
        for key in ("estimated_owners_low", "estimated_owners_high", "estimated_ccu"):
            if key in steamspy:
                comparison[f"steamspy_{key}"] = steamspy[key]

        return {
            "appid": appid,
            "sources": sources,
            "availability": availability,
            "comparison": comparison,
        }, warnings
