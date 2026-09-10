# Security Policy

`steam-mcp` is a **read-only** Model Context Protocol server for Valve's public
Steam APIs plus a disclosed community mirror of current public SteamCMD AppInfo.
This describes its security posture and how to report issues.

## Reporting a vulnerability

Open a private report via
[GitHub Security Advisories](https://github.com/BK927/steam-research-mcp/security/advisories/new),
or a regular issue for non-sensitive reports. Please include steps to reproduce.

## Posture

- **Read-only.** No tool writes, trades, posts, changes status, launches games, or
  makes purchases. Every tool is annotated `readOnlyHint: true`.
- **Bring-your-own-key.** The optional credentials are your Steam Web API key and
  Gamalytic API key, read from `STEAM_API_KEY` and `GAMALYTIC_API_KEY`
  respectively. They are never written to disk, logged, cached, or placed in tool
  output; they are excluded from cache keys, and
  error messages never include either key. The optional `STEAM_USER`
  (a default-to-me convenience) is **not** a credential — it's a public Steam
  profile name and is treated as non-sensitive.
- **Fixed host allowlist.** Most requests go to Valve's
  `api.steampowered.com`, `store.steampowered.com`, and `steamcommunity.com`.
  Current AppInfo/build/branch/depot tools use the explicitly disclosed,
  community-operated `api.steamcmd.net` mirror. The analytics view can also use
  fixed `api.gamalytic.com` and `steamspy.com` endpoints. No tool accepts a URL;
  analytics redirects are rejected and other redirects are checked hop by hop.
  Every other host is rejected (SSRF guard). Steam
  credentials and account identifiers are never sent to the mirror; only the
  requested numeric app ID is. Market price/inventory use Steam's own
  undocumented, rate-limited Community Market endpoints.
- **Input validation.** All tool inputs are typed Pydantic models with
  `extra="forbid"`; identifiers are validated/resolved before use.
- **Rate limiting & resilience.** Per-host token-bucket rate limiting, plus bounded
  retry with exponential backoff (honoring `Retry-After`) on 429/5xx.
- **No MCP-side persistent retention.** Nothing is written to disk. A small
  in-memory TTL cache holds non-account public responses; current AppInfo mirror
  snapshots are cached for five minutes and third-party analytics for up to 24
  hours. Live user data (status, friends,
  wishlist, inventory) is never cached. The independent mirror maintains its own
  AppInfo database, so this is not an end-to-end no-storage data path.

## Out of scope

No write/trade/purchase actions, no account login/OAuth, no SteamDB scraping, no
user-supplied outbound URLs, and no price/CCU/build/AppInfo history. Third-party
analytics are read-only, explicitly selected, and always labeled as estimates.
