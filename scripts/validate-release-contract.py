"""Dependency-free CI checks for the compact release/plugin boundary."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "2.2.0"
EXPECTED_TOOLS = {
    "steam_game_get",
    "steam_player_get",
    "steam_search",
    "steam_reviews_get",
    "steam_community_get",
    "steam_analyze",
    "steam_job_get",
    "steam_job_cancel",
}


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"{path} must contain a JSON object"
    return value


plugin = load_json(ROOT / ".codex-plugin" / "plugin.json")
companion = load_json(ROOT / ".mcp.json")
manifest = load_json(ROOT / "manifest.json")
server = load_json(ROOT / "server.json")
pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")

assert plugin["name"] == "steam-mcp"
assert plugin["version"] == EXPECTED_VERSION
assert plugin["mcpServers"] == "./.mcp.json"
assert set(companion["mcpServers"]) == {"steam-mcp"}
remote = companion["mcpServers"]["steam-mcp"]
assert remote["type"] == "http" and remote["url"].endswith("/mcp")
assert remote["bearer_token_env_var"] == "STEAM_MCP_ACCESS_TOKEN"
assert remote["url"] == "https://steam-mcp.example.com/mcp"
assert 'name = "steam-research-mcp"' in pyproject
assert 'steam-research-mcp = "steam_mcp.server:main"' in pyproject
assert 'steam-mcp = "steam_mcp.server:main"' in pyproject
assert server["name"] == "io.github.BK927/steam-research-mcp"
assert "mcp-name: io.github.BK927/steam-research-mcp" in readme
assert server["repository"]["url"] == "https://github.com/BK927/steam-research-mcp"
assert server["packages"][0]["identifier"] == "steam-research-mcp"

pi_deploy = (ROOT / "scripts" / "deploy-raspberry-pi.sh").read_text(encoding="utf-8")
assert 'PUBLIC_PORT="${STEAM_MCP_PUBLIC_PORT:-8443}"' in pi_deploy
assert 'source "$ENV_FILE"' not in pi_deploy
assert "preserved_env_value MCP_ACCESS_TOKEN" in pi_deploy
assert 'OAUTH_BASE_URL="${STEAM_MCP_OAUTH_BASE_URL:-$PUBLIC_BASE_URL}"' in pi_deploy
assert 'SHARED_HTTPS_PATH="${STEAM_MCP_SHARED_HTTPS_PATH:-}"' in pi_deploy
assert '--set-path="/.well-known/oauth-authorization-server$SHARED_HTTPS_PATH"' in pi_deploy
assert '--set-path="/.well-known/oauth-protected-resource$SHARED_HTTPS_PATH/mcp"' in pi_deploy
assert manifest["version"] == EXPECTED_VERSION
assert server["version"] == EXPECTED_VERSION
assert {tool["name"] for tool in manifest["tools"]} == EXPECTED_TOOLS

docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
assert "python:3.13.11-slim-bookworm@sha256:" in docker
assert "requirements.lock" in docker and "--no-deps" in docker
assert "--require-hashes" in docker and "--no-build-isolation" in docker

runtime_lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
dev_lock = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
assert runtime_lock.count("==") >= 40
assert "hatchling==" in runtime_lock and "--hash=sha256:" in runtime_lock
assert "pytest==9.1.1" in dev_lock and "ruff==0.16.5" in dev_lock
assert "--hash=sha256:" in dev_lock

provision = (ROOT / "scripts" / "provision-gcp.ps1").read_text(encoding="utf-8")
deploy = (ROOT / "scripts" / "deploy-cloud-run.ps1").read_text(encoding="utf-8")
deployment = "\n".join(
    path.read_text(encoding="utf-8")
    for path in (ROOT / "scripts").glob("*gcp*.ps1")
)
deployment += deploy
assert ":latest" not in deployment
assert "$RotateAccessToken" in deployment
assert deploy.count('Add-SecretVersion "steam-mcp-access-token"') == 1
assert 'if ($RotateAccessToken) { Add-SecretVersion "steam-mcp-access-token"' in deploy
assert 'Add-SecretVersion "steam-mcp-access-token"' not in provision
assert "--no-traffic" in deployment and '"$mcpRevision=100"' in deployment
assert '"$workerRevision=100"' in deployment and "--to-tags" not in deployment
assert 'HEALTH_PATH = "/health"' in deployment
assert 'HTTP_MAX_BODY_BYTES = "2097152"' in deployment
assert 'STEAM_MAX_RESULT_BYTES = "12288"' in deployment
assert '"--env-vars-file", $environmentFile' in deployment
assert '"$shortSha-worker-bootstrap-$revisionNonce" "" $workerExists' in deploy
assert '"$shortSha-bootstrap-$revisionNonce" $serviceUrl $bootstrapHosts $workerEndpoint $mcpExists' in deploy
assert "STEAM_CURSOR_SECRET=steam-mcp-cursor-secret:$cursorSecretVersion" in deployment
assert "function Get-ActiveRevision" in deploy
assert "function Restore-Traffic" in deploy
assert '"$workerEndpoint,$workerCandidateEndpoint"' in deploy
assert "$workerPrePromoted = $true" in deploy
assert "Restore-Traffic $WorkerServiceName $previousWorkerRevision" in deploy
assert "Restore-Traffic $ServiceName $previousMcpRevision" in deploy

print("Steam compact release contract passed.")
