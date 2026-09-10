"""Compact Steam MCP v2 runtime for stdio and Google Cloud Run."""

from __future__ import annotations

import inspect
import json
import os
import secrets
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from . import legacy_backend
from .cache import TtlLruCache
from .cloud_jobs import CloudTasksJobRunner, FirestoreJobStore, GcsResultStore
from .cursor import CursorCodec
from .jobs import InlineJobRunner, MemoryJobStore, MemoryResultStore
from .oauth import OAuthRuntime, create_oauth_runtime
from .providers.market_analytics import (
    AnalyticsProviderInput,
    get_gamalytic_analytics,
    get_steamspy_analytics,
)
from .public_server import ServerDependencies, create_server
from .services.base import FunctionBackend, OperationBinding


legacy_mcp = legacy_backend.legacy_mcp


def _public_dependencies() -> ServerDependencies:
    """Assemble the provider adapter and the selected job implementation."""
    operations: dict[str, OperationBinding] = {}
    for tool in legacy_mcp._tool_manager.list_tools():
        signature = inspect.signature(tool.fn, eval_str=True)
        parameter = signature.parameters.get("params")
        if parameter is not None and isinstance(parameter.annotation, type):
            operations[tool.name] = OperationBinding(tool.fn, parameter.annotation)
    operations.update(
        {
            "steam_get_gamalytic_analytics": OperationBinding(
                get_gamalytic_analytics, AnalyticsProviderInput
            ),
            "steam_get_steamspy_analytics": OperationBinding(
                get_steamspy_analytics, AnalyticsProviderInput
            ),
        }
    )

    backend = FunctionBackend(operations)
    cache = TtlLruCache(max_entries=512, ttl_seconds=600)
    job_backend = os.getenv("STEAM_JOB_BACKEND", "memory").strip().lower()
    cursor_secret = os.getenv("STEAM_CURSOR_SECRET", "").encode()
    if job_backend == "gcp" and len(cursor_secret) < 32:
        raise RuntimeError("GCP jobs require STEAM_CURSOR_SECRET with at least 32 bytes")
    if len(cursor_secret) < 32:
        cursor_secret = secrets.token_bytes(32)
    cursor = CursorCodec(cursor_secret, ttl_seconds=86_400)

    if job_backend == "memory":
        job_store = MemoryJobStore(ttl_seconds=86_400, max_jobs=512)
        result_store = MemoryResultStore(max_results=512)
        runner = InlineJobRunner()
    elif job_backend == "gcp":
        ttl = _read_int_env("STEAM_JOB_TTL_SECONDS", 604_800, 86_400, 2_592_000)
        project = os.getenv("GCP_PROJECT", "").strip()
        bucket = os.getenv("STEAM_JOB_BUCKET", "").strip()
        if not project or not bucket:
            raise RuntimeError("GCP jobs require GCP_PROJECT and STEAM_JOB_BUCKET")
        job_store = FirestoreJobStore(
            collection=os.getenv("STEAM_JOB_COLLECTION", "steam_jobs").strip(),
            ttl_seconds=ttl,
        )
        result_store = GcsResultStore(bucket)
        if os.getenv("STEAM_PROCESS_ROLE", "mcp").strip().lower() == "worker":
            runner = InlineJobRunner()
        else:
            worker_url = os.getenv("STEAM_JOB_WORKER_URL", "").strip()
            if not worker_url:
                raise RuntimeError("GCP MCP role requires STEAM_JOB_WORKER_URL")
            runner = CloudTasksJobRunner(
                project=project,
                location=os.getenv("GCP_LOCATION", "asia-northeast1").strip(),
                queue=os.getenv("STEAM_JOB_QUEUE", "steam-analysis").strip(),
                worker_url=worker_url,
                service_account_email=(
                    os.getenv("STEAM_JOB_WORKER_SERVICE_ACCOUNT", "").strip() or None
                ),
                worker_token=os.getenv("STEAM_JOB_WORKER_TOKEN", "").strip() or None,
            )
    else:
        raise RuntimeError("STEAM_JOB_BACKEND must be memory or gcp")

    return ServerDependencies(
        backend=backend,
        cursor=cursor,
        cache=cache,
        job_store=job_store,
        result_store=result_store,
        job_runner=runner,
        status={
            "api_key_configured": legacy_backend._have_api_key(),
            "gamalytic_api_key_configured": bool(
                os.getenv("GAMALYTIC_API_KEY", "").strip()
            ),
            "default_user_configured": bool(legacy_backend._get_default_user()),
            "job_backend": job_backend,
            "process_role": os.getenv("STEAM_PROCESS_ROLE", "mcp").strip().lower(),
            "community_market": os.getenv(
                "STEAM_COMMUNITY_MARKET_STATUS", "experimental"
            ).strip().lower(),
        },
        max_result_bytes=_read_int_env(
            "STEAM_MAX_RESULT_BYTES", 12_288, 4_096, 32_768
        ),
    )


def _read_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _read_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _read_path_env(name: str, default: str) -> str:
    path = os.getenv(name, default).strip()
    if not path.startswith("/") or "?" in path or "#" in path:
        raise RuntimeError(f"{name} must be an absolute URL path")
    return path.rstrip("/") or "/"


def _comma_values(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def _transport_security(host: str) -> Any:
    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts = _comma_values("MCP_ALLOWED_HOSTS")
    allowed_origins = _comma_values("MCP_ALLOWED_ORIGINS")
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    if public_base_url:
        parsed = urlsplit(public_base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeError("PUBLIC_BASE_URL must be an absolute HTTPS URL")
        if parsed.netloc not in allowed_hosts:
            allowed_hosts.append(parsed.netloc)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in allowed_origins:
            allowed_origins.append(origin)

    if host in {"127.0.0.1", "localhost"}:
        for local_host in ("127.0.0.1:*", "localhost:*"):
            if local_host not in allowed_hosts:
                allowed_hosts.append(local_host)
        for local_origin in ("http://127.0.0.1:*", "http://localhost:*"):
            if local_origin not in allowed_origins:
                allowed_origins.append(local_origin)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts),
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _oauth_from_env(access_token: str) -> OAuthRuntime | None:
    if not _read_bool_env("MCP_OAUTH_ENABLED", False):
        return None
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not public_base_url:
        raise RuntimeError("OAuth requires PUBLIC_BASE_URL")
    mcp_path = _read_path_env("MCP_PATH", "/mcp")
    return create_oauth_runtime(
        issuer=os.getenv("MCP_OAUTH_ISSUER", public_base_url).strip(),
        resource=os.getenv("MCP_OAUTH_RESOURCE", f"{public_base_url}{mcp_path}").strip(),
        scope=os.getenv("MCP_OAUTH_SCOPE", "steam.read").strip(),
        login_secret=os.getenv("MCP_OAUTH_LOGIN_SECRET", "").strip(),
        signing_secret=os.getenv("MCP_OAUTH_SIGNING_SECRET", "").strip(),
        access_token=access_token,
        store=os.getenv("MCP_OAUTH_STORE", "memory").strip().lower(),
        project=os.getenv("GCP_PROJECT", "").strip(),
        collection=os.getenv("MCP_OAUTH_CODE_COLLECTION", "steam_oauth_codes").strip(),
    )


# Construct the shared registry only after every environment parser used by the
# dependency factory has been defined. This matters for the Cloud-only GCP job
# branch, which reads its TTL and result-size settings during import.
_access_token = os.getenv("MCP_ACCESS_TOKEN", "").strip()
_oauth = _oauth_from_env(_access_token)
mcp = create_server(_public_dependencies(), _oauth)


class _HttpGateway:
    """Protect the MCP route and add small unauthenticated service endpoints."""

    def __init__(
        self,
        app: Any,
        *,
        mcp_path: str,
        health_path: str,
        access_token: str,
        allow_unauthenticated: bool,
        oauth: OAuthRuntime | None = None,
    ) -> None:
        self.app = app
        self.mcp_path = mcp_path
        self.health_path = health_path
        self.access_token = access_token
        self.allow_unauthenticated = allow_unauthenticated
        self.oauth = oauth

    @staticmethod
    def _headers(scope: dict[str, Any]) -> dict[bytes, bytes]:
        return {key.lower(): value for key, value in scope.get("headers", [])}

    def _authorized(self, scope: dict[str, Any]) -> bool:
        if self.allow_unauthenticated:
            return True
        raw = self._headers(scope).get(b"authorization", b"")
        if not raw.startswith(b"Bearer "):
            return False
        supplied = raw[len(b"Bearer "):].decode("utf-8", errors="ignore")
        return secrets.compare_digest(supplied, self.access_token)

    @staticmethod
    async def _json_response(
        send: Any,
        status: int,
        payload: dict[str, Any],
        *,
        head: bool = False,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        if extra_headers:
            headers.extend(extra_headers)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if head else body})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if path == self.health_path:
            await self._json_response(
                send,
                200,
                {"ok": True, "service": "steam-mcp", "version": __version__},
                head=method == "HEAD",
            )
            return
        if path == "/":
            await self._json_response(
                send,
                200,
                {
                    "service": "steam-mcp",
                    "version": __version__,
                    "transport": "streamable-http",
                    "mcpPath": self.mcp_path,
                    "readOnly": True,
                    "authentication": "oauth2+static-bearer" if self.oauth else (
                        "static-bearer" if self.access_token else "none"
                    ),
                },
                head=method == "HEAD",
            )
            return
        if (
            self.oauth
            and path == "/.well-known/oauth-authorization-server"
            and method in {"GET", "HEAD"}
        ):
            await self._json_response(
                send,
                200,
                self.oauth.provider.authorization_metadata(),
                head=method == "HEAD",
            )
            return
        if (
            self.oauth
            and path
            in {
                "/.well-known/oauth-protected-resource",
                f"/.well-known/oauth-protected-resource{self.mcp_path}",
            }
            and method in {"GET", "HEAD"}
        ):
            await self._json_response(
                send,
                200,
                {
                    "resource": self.oauth.provider.resource,
                    "authorization_servers": [self.oauth.provider.issuer],
                    "scopes_supported": [self.oauth.provider.scope],
                    "bearer_methods_supported": ["header"],
                    "resource_name": "Steam Research MCP Server",
                },
                head=method == "HEAD",
            )
            return
        if (
            not self.oauth
            and path in {self.mcp_path, f"{self.mcp_path}/"}
            and not self._authorized(scope)
        ):
            await self._json_response(
                send,
                401,
                {"error": "unauthorized"},
                head=method == "HEAD",
                extra_headers=[(b"www-authenticate", b"Bearer")],
            )
            return
        await self.app(scope, receive, send)


def _build_http_app() -> tuple[Any, str, int]:
    """Build the role-specific ASGI app without starting a network listener."""
    host = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = _read_int_env("PORT", 8080, 1, 65535)
    mcp_path = _read_path_env("MCP_PATH", "/mcp")
    health_path = _read_path_env("HEALTH_PATH", "/healthz")
    access_token = os.getenv("MCP_ACCESS_TOKEN", "").strip()
    allow_unauthenticated = _read_bool_env("MCP_ALLOW_UNAUTHENTICATED", False)
    process_role = os.getenv("STEAM_PROCESS_ROLE", "mcp").strip().lower()
    if process_role not in {"mcp", "worker"}:
        raise RuntimeError("STEAM_PROCESS_ROLE must be mcp or worker")

    if process_role == "worker":
        from .cloud_jobs import WorkerEndpoint

        worker = WorkerEndpoint(
            mcp._steam_services["analysis"].run_job,
            path="/internal/jobs/run",
            token=os.getenv("STEAM_JOB_WORKER_TOKEN", "").strip() or None,
        )
        return (
            _HttpGateway(
                worker,
                mcp_path=mcp_path,
                health_path=health_path,
                access_token=access_token,
                allow_unauthenticated=True,
                oauth=None,
            ),
            host,
            port,
        )

    if not allow_unauthenticated and not _oauth and len(access_token) < 32:
        raise RuntimeError(
            "HTTP mode requires MCP_ACCESS_TOKEN with at least 32 characters, "
            "unless MCP_ALLOW_UNAUTHENTICATED=true"
        )
    app = mcp.streamable_http_app(
        streamable_http_path=mcp_path,
        stateless_http=True,
        max_request_body_size=_read_int_env(
            "HTTP_MAX_BODY_BYTES", 2_097_152, 1_024, 16_777_216
        ),
        transport_security=_transport_security(host),
        host=host,
    )
    return (
        _HttpGateway(
            app,
            mcp_path=mcp_path,
            health_path=health_path,
            access_token=access_token,
            allow_unauthenticated=allow_unauthenticated,
            oauth=_oauth,
        ),
        host,
        port,
    )


def _run_http() -> None:
    import uvicorn

    app, host, port = _build_http_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if os.getenv("STEAM_PROCESS_ROLE", "mcp").strip().lower() == "worker" and transport == "stdio":
        raise RuntimeError("STEAM_PROCESS_ROLE=worker requires HTTP transport")
    if transport == "stdio":
        mcp.run()
        return
    if transport in {"http", "streamable-http"}:
        _run_http()
        return
    raise RuntimeError("MCP_TRANSPORT must be stdio, http, or streamable-http")


if __name__ == "__main__":
    main()
