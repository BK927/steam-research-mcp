# Changelog

A concise, one-line-per-change history. Versions follow
[Semantic Versioning](https://semver.org/). Releases:
<https://github.com/BK927/steam-research-mcp/releases>

## [2.2.0] — Multi-provider market analytics
- Added `steam_game_get(view="analytics")` without expanding the eight-tool surface, keeping official Steam facts separate from Gamalytic and SteamSpy estimates.
- Added keyless Gamalytic public fields, optional premium-key support, best-effort SteamSpy data, per-provider availability, provenance, and estimate warnings.
- Added least-privilege Cloud Run secret wiring for an optional Gamalytic API key.
- Added a rollback-aware Raspberry Pi systemd/Funnel deployment and public-path analytics smoke test.
- Added a Raspberry Pi Funnel deployment option while retaining Cloud Run as an alternative managed target.
- Moved the Raspberry Pi Funnel default to HTTPS port 8443 for ChatGPT OAuth discovery compatibility and to avoid assuming ownership of shared port 443.
- Fixed Raspberry Pi redeployments so preserved credentials cannot overwrite a newly selected public URL or Funnel port.
- Added an optional shared-port `/steam` OAuth alias for ChatGPT while retaining the dedicated 8443 Steam endpoint for bearer clients.
- Persisted the RFC well-known OAuth discovery routes for the shared `/steam` alias across Raspberry Pi redeployments.
- Kept the `/steam` issuer prefix when the personal OAuth login form submits its access key.

## [2.1.1] — ChatGPT OAuth redirect hotfix
- Allowed the exact ChatGPT callback origin in the login page form policy so Chrome can follow the successful authorization redirect.

## [2.1.0] — Patch B: managed cloud jobs
- Added the GCP job adapter: Cloud Tasks OIDC dispatch to a private same-image worker, Firestore job state, and seven-day Cloud Storage results.
- Added SHA-tag/digest-pinned candidate Cloud Run rollout, explicit bearer rotation, numeric secret versions, smoke gates, promotion, and paired rollback.
- Standardized signed 24-hour cursors, 12 KiB default/32 KiB hard result limits, `/healthz`, and a 2 MiB HTTP request limit.

## [2.0.0] — Patch A: compact MCP surface
- Replaced the legacy 44-tool catalog with eight task-oriented read-only tools. Pre-2.0 tool names are not retained.
- Consolidated game, player, search, review, community, and analysis operations under consistent envelopes, errors, pagination, and job handles.
- Moved to the MCP Python SDK v2-only runtime while preserving local stdio and stateless Streamable HTTP profiles.

## [1.15.0]
- **Review corpus limits are now caller-controlled instead of hard-coded.** `steam_get_app_reviews` accepts up to 100 excerpts and adds `recent_max_reviews`; its fast default remains 600, while `0` follows cursors until the requested date window is fully covered.
- New `steam_get_app_review_batch` exposes Steam's full review payload in cursor pages of up to 100, including author playtime, purchase/free/early-access/Deck flags, helpfulness, edits, and developer responses. Reusing `next_cursor` has no application-level total-review cap.
- New `steam_analyze_app_reviews` streams 5,000 reviews by default (or all with `max_reviews=0`) into sentiment timelines, language/reviewer/playtime distributions, and sentiment split by language, purchase source, free copy, Early Access, Steam Deck, and playtime-at-review. `max_pages`, `max_seconds`, and request failures preserve partial aggregates plus a continuation cursor instead of discarding completed work.
- Review-score semantics are now explicit and correct: `steam_get_app_reviews` and `steam_should_i_buy` report Steam's official all-language, Steam-purchase score separately from the caller-selected/all-readable feedback population. Positive/negative excerpt filters no longer contaminate the score summary.
- Review text is marked as untrusted user-generated content, invisible/control characters are stripped, and public reviewer SteamIDs are omitted by default (`include_author_id=true` opts in). These are prompt-injection/data-minimization mitigations, not a claim of complete content safety.
- Five keyless, stateless SteamDB-style current-state tools were added without scraping SteamDB: `steam_get_product_info`, `steam_get_branches`, `steam_get_depots`, `steam_get_current_build`, and composite `steam_analyze_game`. Current public SteamCMD AppInfo comes from the disclosed community-operated `api.steamcmd.net` mirror; responses include provenance, the MCP keeps only a five-minute memory cache, encrypted manifest values are never exposed, and no historical price/CCU/build/AppInfo claims are made.

## [1.14.0]
- **The API key is now optional, and the server says so.** 15 of the 37 tools never needed a credential — store, games, reviews, prices, deals, tags, live player counts, Steam Deck, achievement rarity — and the three game-finders (`steam_discover`, `steam_should_i_buy`, `steam_recommend`) join them as long as you don't personalize with a `steamid`. `server.json` no longer marks `STEAM_API_KEY` as required (and now declares `STEAM_USER`, which it never did), so clients stop presenting a key as a precondition to installing. README leads with the keyless quickstart.
- When no key is configured, the tools that need one are marked `[unavailable: needs STEAM_API_KEY]` in their descriptions and the finders as `[works without a key unless you pass steamid]`. Without that the model picks an account tool, gets an error, and the server looks broken rather than unconfigured. The markers are omitted entirely when a key *is* present — every tool works then, so they would be pure tokens on every request.
- The "no key configured" error now names the keyless tools to use instead, rather than only saying what's missing.
- The `.mcpb` desktop bundle is registered with the MCP Registry as a second package, so the one-click Claude Desktop install is discoverable there and not only on the releases page. Its `fileSha256` is computed from the artifact attached to the release during publishing, since a hash kept in git goes stale the moment the bundle is rebuilt.

## [1.13.0]
- On the v2 SDK, the "about me" tools can now **ask who you are** instead of failing. Omit a `steamid` with no `STEAM_USER` configured and the server puts one question in front of you; the answer is reused for the rest of the session. It rides the negotiated protocol — a multi-round-trip `tools/call` on 2026-07-28, a push elicitation on 2025-11-25 and earlier — from one code path. The question is invisible to the model (it never enters a tool's input schema), is never asked when the call already names a user or `STEAM_USER` is set, and is never asked of a client that hasn't declared the elicitation capability — those clients keep today's "set STEAM_USER" error exactly as before, as does declining the question. The keyless game-finders (`steam_discover` / `steam_should_i_buy` / `steam_recommend`) still treat an omitted `steamid` as "don't personalize" and never ask.
- Switched our HTTP client from `httpx` to **`httpx2`**, which is what the v2 SDK uses — a fresh install now carries one HTTP stack instead of two. Note that httpx2 verifies TLS against the **operating system trust store** (via truststore) rather than certifi's bundled CA list: a minimal container with no system CA store, or a private CA that only certifi knew about, needs `SSL_CERT_FILE`/`SSL_CERT_DIR` set. Our API-key log scrubbing now covers the `httpx2`/`httpcore2` logger names as well as the old ones.

## [1.12.0]
- **Fixes a broken install.** The MCP Python SDK v2 removed `mcp.server.fastmcp` outright (FastMCP is now `MCPServer` under `mcp.server.mcpserver`), and our unbounded `mcp>=1.2.0` meant a fresh `uvx steam-mcp` picked up v2 and died with `ModuleNotFoundError`. The server now runs on **both** SDK majors — v2 (spec revision 2026-07-28) and the v1.x maintenance line — and the requirement is `mcp>=1.28`.
- On the v2 SDK the server speaks spec revision **2026-07-28** (stateless: no `initialize` handshake, no session id) and reports its own `version` in `serverInfo`. On v1.x it keeps using the `initialize` handshake, which modern clients still fall back to after probing `server/discover`.
- Cache freshness hints (SEP-2549, v2 SDK only): `tools/list`, `prompts/list`, `resources/list`, `resources/templates/list` advertise a 1-hour TTL and `resources/read` 10 minutes, all `public`. Our listings are static for the life of the process, so clients can stop re-fetching ~58 KB of `tools/list` on every reconnect.

## [1.11.0]
- New optional `STEAM_USER` config (set it next to your API key to your Steam vanity name / ID / profile URL). The "about me" tools — library, owned games, achievements, wishlist, friends, inventory, level, bans, badges, groups, co-op night, compare — now default to you when you omit the `steamid`, so you don't have to paste your ID every time. Passing a `steamid` still overrides. The keyless game-finders (discover / should_i_buy / recommend) keep personalization explicit.

## [1.10.0]
- `steam_plan_coop_night` gains `mode="new"` — recommend well-reviewed co-op games that NONE of the group owns yet (fresh picks to buy together), vs the default `mode="owned"` (games you already share). (Filtering which friends to include already works via the `friends` list.)

## [1.9.0]
- `steam_discover` gains a `released_within_days` filter — "what came out in the last N days" matching your tags/price/taste (newest-first). Release dates ride in the existing batched GetItems call, so no extra requests and negligible token cost.

## [1.8.3]
- Sharper descriptions on the overlapping game-finding tools (search / discover / recommend / should_i_buy / find-friends) with explicit "use this / not that" boundaries, to reduce wrong-tool selection. README notes Claude Code `--scope`.

## [1.8.2]
- Privacy-aware errors: when a profile or sub-setting is private, each tool names the exact Steam setting to make Public (Game details / Friends List / Inventory / My profile) and links the settings page.

## [1.8.1]
- Batched price lookups — one `GetItems` call per ~50 appids for wishlist / DLC / discover / recommend (fewer requests, less rate-limiting). Internal only.

## [1.8.0]
- Steam Deck compatibility: new `steam_get_deck_compatibility` tool + Deck rating inline in `steam_get_app_details` (37 tools).

## [1.7.7]
- ReDoS guard: cap input length before the HTML / CS-name / temp-client regexes.
- `/profiles/<id>` URLs must carry a valid SteamID64.

## [1.7.6]
- SSRF allowlist now enforced on every redirect hop, not just the first URL.
- API-key scrubbing extended to `SteamApiError` messages.

## [1.7.5]
- No "truncated" nag when a list limit is explicitly 0.

## [1.7.4]
- Temp-client matcher catches all-caps / mid-string "beta" (e.g. "REMATCH BETA TEST") and more build/test markers.

## [1.7.3]
- Cross-tool sweep: taste-seeding (recommend / discover / should_i_buy) and co-op night now drop non-retail clients.

## [1.7.2]
- `analyze_library` header shows the persona name (+ `persona_name` in JSON); clearer average wording.

## [1.7.1]
- Launched-but-tiny playtime renders `<0.1h` instead of a contradictory `0.0h` (+ `hours_str`).

## [1.7.0]
- `steam_analyze_library` excludes non-retail clients (betas/playtests/demos) by default — new `exclude_temp_clients`.

## [1.6.1]
- Abandoned-list header always shows `(N total, showing M)`, matching the Backlog header.

## [1.6.0]
- Abandoned list surfaces recently-dropped games first — new `abandoned_sort` (recent / oldest / playtime).

## [1.5.0]
- `backlog_limit` no longer truncates the Abandoned list — new independent `abandoned_limit`.

## [1.4.3]
- `analyze_library` backlog no longer an alphabetical slice: `backlog_limit` defaults to 100, with a `backlog_truncated` flag.

## [1.4.2]
- Keep the API key out of logs (quiet the httpx/httpcore loggers).

## [1.4.1]
- Bundle icon + expanded PRIVACY.md (Connectors Directory prep).

## [1.4.0]
- ~88% smaller tool descriptions on the wire; host allowlist, per-host rate limiting, API-key scrubbing; SECURITY.md + SKILL.md.

## [1.3.0]
- `steam_get_market_price` — Community Market price for an item (type/rarity + CS2 condition).

## [1.2.0]
- `steam_get_inventory` — a user's game or Steam Community inventory.

## [1.1.0]
- `steam_get_app_regional_pricing`, `steam_get_workshop_item`, `steam_get_user_groups`.

## [1.0.0]
- First stable release — public surface under a SemVer stability contract; retry with backoff; broader caching.

## [0.12.0]
- MCP prompts + resources + localization (`language` parameter).

## [0.11.0]
- `steam_plan_coop_night`.

## [0.10.0]
- `steam_should_i_buy`, `steam_recommend`.

## [0.9.0]
- `steam_discover`.

## [0.8.1]
- Loop-aware shared httpx client fix; CI (ruff + pytest across 3.10–3.13).

## [0.8.0]
- `steam_find_friends_who_own`, `steam_get_rarest_unlocks`, `steam_get_app_tags`.

## [0.7.0]
- `steam_get_dlc`, `steam_get_user_game_stats`; pooled httpx client + bounded concurrent fan-out; international price formatting.

## [0.6.0]
- In-memory TTL cache for static responses; test suite.

## [0.5.0]
- `steam_analyze_library`; comprehensive `steam_get_app_details`.

## [0.4.0]
- `steam_get_player_badges`, `steam_get_package_details`, `steam_compare_players`.

## [0.3.0]
- `steam_get_store_highlights`, `steam_get_wishlist`; recent-reviews filter.

## [0.2.0]
- Initial public release — 16 read-only tools; BYOK; `.mcpb` + PyPI.
