#!/usr/bin/env bash
set -euo pipefail

APP_ID="steam-mcp"
APP_COMMIT="${STEAM_MCP_COMMIT:?Set STEAM_MCP_COMMIT to the exact source commit}"
SOURCE_REPOSITORY="${STEAM_MCP_SOURCE_REPOSITORY:-BK927/steam-research-mcp}"
PUBLIC_BASE_URL="${STEAM_MCP_PUBLIC_BASE_URL:?Set STEAM_MCP_PUBLIC_BASE_URL to the public HTTPS origin}"
LOCAL_PORT="${STEAM_MCP_LOCAL_PORT:-8082}"
PUBLIC_PORT="${STEAM_MCP_PUBLIC_PORT:-8443}"
OAUTH_BASE_URL="${STEAM_MCP_OAUTH_BASE_URL:-$PUBLIC_BASE_URL}"
SHARED_HTTPS_PATH="${STEAM_MCP_SHARED_HTTPS_PATH:-}"

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "This deployment expects aarch64." >&2
  exit 1
fi
if [[ ! "$APP_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "STEAM_MCP_COMMIT must be an exact 40-character commit SHA." >&2
  exit 1
fi
if [[ ! "$PUBLIC_BASE_URL" =~ ^https://[^/]+:${PUBLIC_PORT}$ ]]; then
  echo "STEAM_MCP_PUBLIC_BASE_URL must be an HTTPS origin ending in :$PUBLIC_PORT." >&2
  exit 1
fi
if [[ ! "$OAUTH_BASE_URL" =~ ^https://[^/?#]+(/[^?#]+)?$ ]]; then
  echo "STEAM_MCP_OAUTH_BASE_URL must be an absolute HTTPS URL without a query or fragment." >&2
  exit 1
fi
if [[ -n "$SHARED_HTTPS_PATH" ]]; then
  if [[ ! "$SHARED_HTTPS_PATH" =~ ^/[A-Za-z0-9._~-]+$ ]]; then
    echo "STEAM_MCP_SHARED_HTTPS_PATH must be one simple absolute path segment." >&2
    exit 1
  fi
  if [[ "$OAUTH_BASE_URL" != *"$SHARED_HTTPS_PATH" ]]; then
    echo "STEAM_MCP_OAUTH_BASE_URL must end in STEAM_MCP_SHARED_HTTPS_PATH." >&2
    exit 1
  fi
fi
if [[ ! "$LOCAL_PORT" =~ ^[0-9]+$ ]] || ((LOCAL_PORT < 1024 || LOCAL_PORT > 65535)); then
  echo "STEAM_MCP_LOCAL_PORT must be an unprivileged TCP port." >&2
  exit 1
fi

ROOT="$HOME/services/$APP_ID"
RELEASE="$ROOT/releases/$APP_COMMIT"
CONFIG_ROOT="$HOME/.config/$APP_ID"
ENV_FILE="$CONFIG_ROOT/env"
UNIT_ROOT="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_ROOT/$APP_ID.service"
CURRENT="$ROOT/current"
PUBLIC_HOST="${PUBLIC_BASE_URL#https://}"
OAUTH_HOST="${OAUTH_BASE_URL#https://}"
OAUTH_HOST="${OAUTH_HOST%%/*}"
OAUTH_ORIGIN="https://$OAUTH_HOST"
PREVIOUS_RELEASE="$(readlink -f "$CURRENT" 2>/dev/null || true)"
ACTIVATED=false
FUNNEL_CONFIGURED=false

umask 077
mkdir -p "$ROOT/releases" "$CONFIG_ROOT" "$UNIT_ROOT"

work_dir="$(mktemp -d)"
cleanup() { rm -rf "$work_dir"; }
rollback() {
  status=$?
  if [[ "$status" -ne 0 && "$ACTIVATED" == "true" ]]; then
    echo "Deployment failed after activation; restoring the previous Steam release." >&2
    if [[ -n "$PREVIOUS_RELEASE" && -d "$PREVIOUS_RELEASE" ]]; then
      ln -sfn "$PREVIOUS_RELEASE" "$CURRENT"
      systemctl --user restart "$APP_ID.service" || true
    else
      systemctl --user disable --now "$APP_ID.service" || true
    fi
  fi
  if [[ "$status" -ne 0 && "$FUNNEL_CONFIGURED" == "true" ]]; then
    echo "Restoring the previous Tailscale Serve/Funnel configuration." >&2
    tailscale serve set-config --all "$work_dir/tailscale-before.json" || true
  fi
  cleanup
  exit "$status"
}
trap rollback EXIT

if [[ ! -x "$RELEASE/.venv/bin/python" ]]; then
  rm -rf "$RELEASE"
  mkdir -p "$RELEASE"
  curl --fail --silent --show-error --location \
    "https://github.com/${SOURCE_REPOSITORY}/archive/${APP_COMMIT}.tar.gz" \
    --output "$work_dir/source.tar.gz"
  tar -xzf "$work_dir/source.tar.gz" --strip-components=1 -C "$RELEASE"
  python3 -m venv "$RELEASE/.venv"
  "$RELEASE/.venv/bin/python" -m pip install --disable-pip-version-check \
    --no-cache-dir --require-hashes -r "$RELEASE/requirements.lock"
  "$RELEASE/.venv/bin/python" -m pip install --disable-pip-version-check \
    --no-cache-dir --no-build-isolation --no-deps "$RELEASE"
fi

preserved_env_value() {
  local key="$1"
  local line=""
  if [[ -f "$ENV_FILE" ]]; then
    line="$(grep -m1 -E "^${key}=" "$ENV_FILE" || true)"
  fi
  printf '%s' "${line#*=}"
}

# Preserve credentials and stable signing secrets without importing stale
# deployment settings such as PUBLIC_BASE_URL or the selected Funnel port.
STEAM_API_KEY="${STEAM_API_KEY:-$(preserved_env_value STEAM_API_KEY)}"
GAMALYTIC_API_KEY="${GAMALYTIC_API_KEY:-$(preserved_env_value GAMALYTIC_API_KEY)}"
STEAM_USER="${STEAM_USER:-$(preserved_env_value STEAM_USER)}"
MCP_ACCESS_TOKEN="${MCP_ACCESS_TOKEN:-$(preserved_env_value MCP_ACCESS_TOKEN)}"
STEAM_CURSOR_SECRET="${STEAM_CURSOR_SECRET:-$(preserved_env_value STEAM_CURSOR_SECRET)}"
MCP_OAUTH_LOGIN_SECRET="${MCP_OAUTH_LOGIN_SECRET:-$(preserved_env_value MCP_OAUTH_LOGIN_SECRET)}"
MCP_OAUTH_SIGNING_SECRET="${MCP_OAUTH_SIGNING_SECRET:-$(preserved_env_value MCP_OAUTH_SIGNING_SECRET)}"
MCP_ACCESS_TOKEN="${MCP_ACCESS_TOKEN:-$(openssl rand -hex 32)}"
STEAM_CURSOR_SECRET="${STEAM_CURSOR_SECRET:-$(openssl rand -hex 32)}"
MCP_OAUTH_LOGIN_SECRET="${MCP_OAUTH_LOGIN_SECRET:-$(openssl rand -hex 32)}"
MCP_OAUTH_SIGNING_SECRET="${MCP_OAUTH_SIGNING_SECRET:-$(openssl rand -hex 32)}"

env_tmp="$(mktemp "$CONFIG_ROOT/env.XXXXXX")"
cat >"$env_tmp" <<EOF
STEAM_API_KEY=$STEAM_API_KEY
GAMALYTIC_API_KEY=$GAMALYTIC_API_KEY
STEAM_USER=$STEAM_USER
STEAM_CURSOR_SECRET=$STEAM_CURSOR_SECRET
STEAM_CURSOR_TTL_SECONDS=86400
STEAM_MAX_RESULT_BYTES=12288
STEAM_JOB_BACKEND=memory
STEAM_PROCESS_ROLE=mcp
STEAM_COMMUNITY_MARKET_STATUS=degraded
MCP_TRANSPORT=http
HOST=127.0.0.1
PORT=$LOCAL_PORT
MCP_PATH=/mcp
HEALTH_PATH=/healthz
HTTP_MAX_BODY_BYTES=2097152
MCP_ACCESS_TOKEN=$MCP_ACCESS_TOKEN
MCP_ALLOW_UNAUTHENTICATED=false
PUBLIC_BASE_URL=$OAUTH_BASE_URL
MCP_ALLOWED_HOSTS=$PUBLIC_HOST,$OAUTH_HOST
MCP_ALLOWED_ORIGINS=$PUBLIC_BASE_URL,$OAUTH_ORIGIN
MCP_OAUTH_ENABLED=true
MCP_OAUTH_ISSUER=$OAUTH_BASE_URL
MCP_OAUTH_RESOURCE=$OAUTH_BASE_URL/mcp
MCP_OAUTH_SCOPE=steam.read
MCP_OAUTH_LOGIN_SECRET=$MCP_OAUTH_LOGIN_SECRET
MCP_OAUTH_SIGNING_SECRET=$MCP_OAUTH_SIGNING_SECRET
MCP_OAUTH_STORE=memory
EOF
chmod 0600 "$env_tmp"
mv -f "$env_tmp" "$ENV_FILE"

unit_tmp="$(mktemp "$UNIT_ROOT/$APP_ID.service.XXXXXX")"
cat >"$unit_tmp" <<EOF
[Unit]
Description=Steam MCP (Raspberry Pi)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$CURRENT
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$ENV_FILE
ExecStart=$CURRENT/.venv/bin/python -m steam_mcp.server
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
MemoryMax=768M

[Install]
WantedBy=default.target
EOF
chmod 0600 "$unit_tmp"
mv -f "$unit_tmp" "$UNIT_FILE"

ln -sfn "$RELEASE" "$CURRENT"
systemctl --user daemon-reload
systemctl --user enable "$APP_ID.service"
ACTIVATED=true
systemctl --user restart "$APP_ID.service"

for attempt in $(seq 1 45); do
  if curl --fail --silent --show-error \
    --header "Host: $PUBLIC_HOST" \
    "http://127.0.0.1:$LOCAL_PORT/healthz" >/dev/null; then
    break
  fi
  if [[ "$attempt" == "45" ]]; then
    systemctl --user status "$APP_ID.service" --no-pager >&2 || true
    journalctl --user -u "$APP_ID.service" -n 100 --no-pager >&2 || true
    exit 1
  fi
  sleep 1
done

status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header "Host: $PUBLIC_HOST" "http://127.0.0.1:$LOCAL_PORT/mcp")"
if [[ "$status" != "401" ]]; then
  echo "Expected unauthenticated local /mcp to return 401, got $status." >&2
  exit 1
fi

curl --fail --silent --show-error \
  --header "Host: $PUBLIC_HOST" \
  "http://127.0.0.1:$LOCAL_PORT/.well-known/oauth-authorization-server" >/dev/null

tailscale serve get-config --all "$work_dir/tailscale-before.json"
tailscale funnel --bg --https="$PUBLIC_PORT" --yes "http://127.0.0.1:$LOCAL_PORT" >/dev/null
FUNNEL_CONFIGURED=true
if [[ -n "$SHARED_HTTPS_PATH" ]]; then
  tailscale funnel --bg --https=443 --set-path="$SHARED_HTTPS_PATH" --yes \
    "http://127.0.0.1:$LOCAL_PORT" >/dev/null
  tailscale funnel --bg --https=443 \
    --set-path="/.well-known/oauth-authorization-server$SHARED_HTTPS_PATH" --yes \
    "http://127.0.0.1:$LOCAL_PORT/.well-known/oauth-authorization-server" >/dev/null
  tailscale funnel --bg --https=443 \
    --set-path="/.well-known/oauth-protected-resource$SHARED_HTTPS_PATH/mcp" --yes \
    "http://127.0.0.1:$LOCAL_PORT/.well-known/oauth-protected-resource/mcp" >/dev/null
fi

for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error "$PUBLIC_BASE_URL/healthz" >/dev/null; then
    break
  fi
  if [[ "$attempt" == "30" ]]; then
    tailscale funnel status >&2 || true
    exit 1
  fi
  sleep 1
done

MCP_SMOKE_ACCESS_TOKEN="$MCP_ACCESS_TOKEN" \
  "$RELEASE/.venv/bin/python" "$RELEASE/scripts/smoke-raspberry-pi.py" \
  --url "$PUBLIC_BASE_URL/mcp"

ACTIVATED=false
FUNNEL_CONFIGURED=false
echo "DEPLOYED_COMMIT=$APP_COMMIT"
echo "LOCAL_HEALTH=ok"
echo "UNAUTHENTICATED_MCP=$status"
echo "OAUTH_DISCOVERY=ok"
echo "PUBLIC_MCP=$PUBLIC_BASE_URL/mcp"
echo "OAUTH_MCP=$OAUTH_BASE_URL/mcp"
