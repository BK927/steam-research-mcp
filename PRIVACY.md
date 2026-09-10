# Privacy Policy — Steam MCP

_Last updated: 2026-09-04_

Steam MCP is a read-only, self-hosted Model Context Protocol server. You may run
it locally or in infrastructure you control; the project author does not operate a
hosted Steam MCP service. This document explains what it does and does not do with
data.

## What it accesses

When you (or your AI client) invoke a tool, the server makes read-only HTTPS
requests to fixed, allowlisted endpoints:

- `https://api.steampowered.com` (Valve's Steam Web API)
- `https://store.steampowered.com` (Valve's Steam storefront/reviews)
- `https://steamcommunity.com` (Valve's public community/market endpoints)
- `https://api.steamcmd.net` (a community-operated mirror of public SteamCMD
  AppInfo, used only by current product/build/branch/depot tools)
- `https://api.gamalytic.com` (third-party market estimates, used only by the
  explicitly requested analytics view)
- `https://steamspy.com` (third-party sample estimates, used only by the
  explicitly requested analytics view)

Requests may include:

- **Your Steam Web API key**, read from the key you supply at install time (or a
  local `.env` / environment variable). The key is sent only to Valve, only as
  required to authenticate API calls. It is never transmitted anywhere else.
- **Your optional Gamalytic API key**, sent only to `api.gamalytic.com` when you
  request Gamalytic analytics. Without one, the MCP requests only the provider's
  keyless public field subset.
- **SteamIDs / vanity names / app IDs** you ask about, passed as request
  parameters to Valve where the requested tool requires them.
- **A numeric app ID** sent to `api.steamcmd.net` only when you invoke a current
  AppInfo/build/branch/depot tool (or `steam_analyze_game`). Steam API keys,
  SteamIDs, libraries, review corpora, and other account data are never sent to
  that mirror.
- **A numeric app ID** sent to Gamalytic and/or SteamSpy only when those providers
  are selected in `steam_game_get(view="analytics")`. Steam credentials, profile
  identifiers, libraries, and review text are never sent to either provider.
- **Public review data** returned by Valve, which can include review text, public
  reviewer SteamIDs, playtime, helpfulness votes, purchase/free/early-access flags,
  and developer responses. Review pages are processed in memory and are not
  persisted. Returned reviewer SteamIDs are omitted by default; callers must
  explicitly set `include_author_id=true` when an analysis genuinely needs them.

## What it does NOT do

- It does **not** transmit data to the author and contains no analytics or
  telemetry. Most data goes directly between your MCP deployment and Valve. The
  third-party AppInfo and analytics providers receive only the requested numeric
  app ID on the explicitly selected views described above.
- It is **read-only**: it cannot send messages, change your status, modify your
  account, launch games, or make purchases.
- It does **not** write your API keys to any file it ships. The keys stay in your
  local configuration.

## Storage, sharing, and retention

- **Local storage:** none. The MCP persists nothing to disk. A small in-memory
  cache may hold public, non-account store/app/tag/news/review-summary responses
  and current AppInfo/analytics snapshots; it lives only for the running process.
  AppInfo mirror responses are cached for five minutes and third-party analytics
  for up to 24 hours.
- **Third-party sharing:** only a requested numeric app ID is sent to the disclosed
  AppInfo/analytics providers. They are independent services with their own logging
  and retention practices. No Steam credential or account identifier is sent to
  them by this MCP. A Gamalytic credential is sent only to Gamalytic itself.
- **Local retention:** nothing is retained between process restarts. Large review
  scans keep aggregate counters and bounded representative samples rather than the
  complete corpus. Invisible/control characters are removed from returned review
  and developer-response text.
- **If the server asks who you are:** when no `STEAM_USER` is configured and your
  client supports elicitation, the server may ask once for your Steam account so
  the "about me" tools have someone to talk about. That answer is held in memory
  for the life of the process — so you are asked once rather than once per call —
  and is never written to disk or sent anywhere but Valve's endpoints. It is a
  public profile name, not a credential. Restarting the server forgets it; setting
  `STEAM_USER` stops it being asked at all.

## Data visibility

The account tools can only read data that Valve exposes. Friends lists, owned
games, and achievements are returned only when the target Steam profile's privacy
settings make them **Public**. Store reviews and their accompanying reviewer
metadata are public storefront data. The AppInfo mirror exposes current public
SteamCMD metadata and may report that an access token is missing for restricted
apps; the MCP cannot bypass that restriction or access private profile data.

## Your responsibilities

- Keep your Steam Web API and Gamalytic API keys secret. Treat them like passwords.
- Your use of the Steam Web API is governed by Valve's
  [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms).

## Contact

Issues and questions: https://github.com/BK927/steam-research-mcp/issues

_This project is not affiliated with or endorsed by Valve Corporation._
