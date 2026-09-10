#!/usr/bin/env python3
"""Add the released .mcpb bundle to server.json as a second package.

The MCP Registry can serve more than one package per server. We publish to PyPI
(`uvx steam-research-mcp`) and also attach a `.mcpb` desktop bundle to each GitHub
Release; registering the bundle too makes the one-click Claude Desktop install
discoverable from the registry instead of only from the releases page.

The registry requires a `fileSha256` for a bundle, and it has to be the hash of
the artifact people actually download. That rules out keeping it in git, where it
would go stale the moment the bundle is rebuilt — so this runs during the publish
workflow and hashes the file attached to the release being published.

Doing nothing is always a valid outcome: no bundle attached, or a release whose
assets aren't up yet, just means we publish PyPI-only exactly as before. The
script never fails the release over a missing bundle; it only fails on a bundle
it cannot verify, because publishing a wrong hash is worse than publishing none.

Usage: add_mcpb_package.py <release-tag>
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import urllib.error
import urllib.request

REPO = "BK927/steam-research-mcp"
SERVER_JSON = pathlib.Path(__file__).resolve().parent.parent / "server.json"
TIMEOUT = 60


def _asset_url(tag: str) -> str:
    return f"https://github.com/{REPO}/releases/download/{tag}/steam-mcp.mcpb"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <release-tag>", file=sys.stderr)
        return 2
    tag = argv[1]
    url = _asset_url(tag)

    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"No .mcpb attached to {tag} — publishing PyPI-only.")
            return 0
        print(f"Could not fetch {url}: HTTP {e.code}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Could not fetch {url}: {e}", file=sys.stderr)
        return 1

    if not blob:
        print(f"{url} is empty — refusing to register it.", file=sys.stderr)
        return 1

    digest = hashlib.sha256(blob).hexdigest()
    doc = json.loads(SERVER_JSON.read_text())
    version = doc["version"]

    packages = [p for p in doc.get("packages", [])
                if p.get("registryType") != "mcpb"]
    pypi = next((p for p in packages if p.get("registryType") == "pypi"), None)
    # The desktop bundle takes the same configuration as the PyPI package, so
    # mirror its environment variables rather than restating them and letting the
    # two descriptions drift apart.
    env = pypi.get("environmentVariables", []) if pypi else []

    packages.append({
        "registryType": "mcpb",
        "identifier": url,
        "version": version,
        "fileSha256": digest,
        "transport": {"type": "stdio"},
        "environmentVariables": env,
    })
    doc["packages"] = packages
    SERVER_JSON.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"Registered .mcpb for {tag} ({len(blob)} bytes, sha256 {digest[:16]}…)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
