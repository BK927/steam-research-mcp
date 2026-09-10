"""Personal OAuth 2.1 authorization server for the hosted Steam MCP."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response


CHATGPT_STABLE_CLIENT_ID = "https://chatgpt.com/oauth/client.json"
CHATGPT_STABLE_REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
CHATGPT_CLIENT_ID_RE = re.compile(r"^https://chatgpt\.com/oauth/([^/]+)/client\.json$")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthorizationCodeStore(Protocol):
    async def put(self, code: str, value: dict[str, Any]) -> None: ...

    async def get(self, code: str) -> dict[str, Any] | None: ...

    async def consume(self, code: str) -> dict[str, Any] | None: ...


class MemoryAuthorizationCodeStore:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def put(self, code: str, value: dict[str, Any]) -> None:
        async with self._lock:
            self._items[_hash(code)] = dict(value)

    async def get(self, code: str) -> dict[str, Any] | None:
        async with self._lock:
            value = self._items.get(_hash(code))
            return dict(value) if value else None

    async def consume(self, code: str) -> dict[str, Any] | None:
        async with self._lock:
            value = self._items.pop(_hash(code), None)
            return dict(value) if value else None


class FirestoreAuthorizationCodeStore:
    """Firestore-backed one-time codes shared by Cloud Run instances."""

    def __init__(self, project: str, collection: str) -> None:
        from google.cloud import firestore

        self._firestore = firestore
        self._client = firestore.Client(project=project or None)
        self._collection = collection

    def _document(self, code: str) -> Any:
        return self._client.collection(self._collection).document(_hash(code))

    async def put(self, code: str, value: dict[str, Any]) -> None:
        stored = dict(value)
        stored["delete_at"] = datetime.fromtimestamp(
            int(value["expires_at"]), tz=timezone.utc
        )
        await asyncio.to_thread(self._document(code).set, stored)

    async def get(self, code: str) -> dict[str, Any] | None:
        snapshot = await asyncio.to_thread(self._document(code).get)
        return snapshot.to_dict() if snapshot.exists else None

    async def consume(self, code: str) -> dict[str, Any] | None:
        document = self._document(code)
        transaction = self._client.transaction()
        firestore = self._firestore

        @firestore.transactional
        def consume_in_transaction(txn: Any) -> dict[str, Any] | None:
            snapshot = document.get(transaction=txn)
            if not snapshot.exists:
                return None
            txn.delete(document)
            return snapshot.to_dict()

        return await asyncio.to_thread(consume_in_transaction, transaction)


@dataclass(frozen=True)
class OAuthRuntime:
    provider: "PersonalOAuthProvider"
    settings: AuthSettings


class PersonalOAuthProvider:
    """A single-user OAuth provider restricted to ChatGPT's published clients."""

    def __init__(
        self,
        *,
        issuer: str,
        resource: str,
        scope: str,
        login_secret: str,
        signing_secret: str,
        access_token: str,
        code_store: AuthorizationCodeStore,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.resource = resource.rstrip("/")
        self.scope = scope
        self.login_secret = login_secret
        self.signing_secret = signing_secret.encode("utf-8")
        self.static_access_token = access_token
        self.code_store = code_store

    def authorization_metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "scopes_supported": [self.scope],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
        }

    def _sign(self, claims: dict[str, Any]) -> str:
        header = _b64url(b'{"alg":"HS256","typ":"JWT"}')
        payload = _b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        signature = _b64url(hmac.new(self.signing_secret, f"{header}.{payload}".encode(), hashlib.sha256).digest())
        return f"{header}.{payload}.{signature}"

    def _verify(self, token: str, token_use: str) -> dict[str, Any] | None:
        try:
            header, payload, signature = token.split(".")
            expected = _b64url(
                hmac.new(self.signing_secret, f"{header}.{payload}".encode(), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(signature, expected):
                return None
            claims = json.loads(_b64url_decode(payload))
            now = int(time.time())
            if (
                claims.get("token_use") != token_use
                or claims.get("iss") != self.issuer
                or claims.get("aud") != self.resource
                or int(claims.get("exp", 0)) <= now
            ):
                return None
            return claims
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def _client_redirects(self, client_id: str) -> list[str] | None:
        if client_id == CHATGPT_STABLE_CLIENT_ID:
            return [CHATGPT_STABLE_REDIRECT_URI]
        match = CHATGPT_CLIENT_ID_RE.fullmatch(client_id)
        if match:
            return [f"https://chatgpt.com/connector/oauth/{match.group(1)}"]
        return None

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        redirects = self._client_redirects(client_id)
        if not redirects:
            return None
        return OAuthClientInformationFull(
            client_id=client_id,
            redirect_uris=redirects,
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=self.scope,
            client_name="ChatGPT",
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource != self.resource:
            raise AuthorizeError("invalid_target", "The resource does not identify this MCP server.")
        now = int(time.time())
        transaction = self._sign(
            {
                "token_use": "authorization_request",
                "iss": self.issuer,
                "aud": self.resource,
                "iat": now,
                "exp": now + 300,
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "scope": params.scopes or [self.scope],
                "state": params.state,
                "code_challenge": params.code_challenge,
            }
        )
        return f"{self.issuer}/oauth/login?{urlencode({'transaction': transaction})}"

    async def login(self, request: Request) -> Response:
        transaction = request.query_params.get("transaction", "")
        error = ""
        if request.method == "POST":
            body = await request.body()
            if len(body) > 16_384:
                return Response("Request too large", status_code=413)
            form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
            transaction = form.get("transaction", [""])[0]
            supplied = form.get("access_key", [""])[0]
            if not hmac.compare_digest(_hash(supplied), _hash(self.login_secret)):
                error = "The access key was not accepted."
            else:
                claims = self._verify(transaction, "authorization_request")
                if not claims:
                    return Response("Authorization request expired or invalid", status_code=400)
                code = secrets.token_urlsafe(32)
                await self.code_store.put(
                    code,
                    {
                        "code": code,
                        "client_id": claims["client_id"],
                        "scopes": claims["scope"],
                        "expires_at": int(time.time()) + 300,
                        "code_challenge": claims["code_challenge"],
                        "redirect_uri": claims["redirect_uri"],
                        "redirect_uri_provided_explicitly": claims[
                            "redirect_uri_provided_explicitly"
                        ],
                        "resource": self.resource,
                        "subject": "personal",
                    },
                )
                return RedirectResponse(
                    construct_redirect_uri(
                        claims["redirect_uri"],
                        code=code,
                        state=claims.get("state"),
                        iss=self.issuer,
                    ),
                    status_code=302,
                    headers={"Cache-Control": "no-store"},
                )

        if not self._verify(transaction, "authorization_request"):
            return Response("Authorization request expired or invalid", status_code=400)
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect Steam Research MCP Server</title><style>body{{font:16px system-ui;max-width:32rem;margin:12vh auto;padding:1.5rem;color:#17202a}}input,button{{box-sizing:border-box;width:100%;padding:.8rem;margin:.4rem 0}}button{{cursor:pointer}}.error{{color:#b42318}}</style></head>
<body><h1>Connect Steam Research MCP Server</h1><p>Enter the private access key for this personal server.</p>{error_html}
<form method="post" action="{html.escape(f'{self.issuer}/oauth/login', quote=True)}"><input type="hidden" name="transaction" value="{html.escape(transaction, quote=True)}">
<label>Access key<input type="password" name="access_key" autocomplete="current-password" required autofocus></label>
<button type="submit">Authorize ChatGPT</button></form></body></html>"""
        return HTMLResponse(
            page,
            status_code=401 if error else 200,
            headers={
                "Cache-Control": "no-store",
                # Chrome applies form-action to redirects after a form POST. The
                # successful login response redirects to ChatGPT's OAuth
                # callback, so that origin must be allowed explicitly.
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://chatgpt.com; frame-ancestors 'none'; base-uri 'none'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @staticmethod
    def _authorization_code(value: dict[str, Any]) -> AuthorizationCode:
        return AuthorizationCode.model_validate(value)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        value = await self.code_store.get(authorization_code)
        if not value or value.get("client_id") != client.client_id:
            return None
        return self._authorization_code(value)

    def _tokens(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = int(time.time())
        common = {
            "iss": self.issuer,
            "aud": self.resource,
            "sub": "personal",
            "client_id": client_id,
            "scope": " ".join(scopes),
            "iat": now,
        }
        access = self._sign({**common, "token_use": "access", "exp": now + 3600, "jti": secrets.token_urlsafe(16)})
        refresh = self._sign({**common, "token_use": "refresh", "exp": now + 2_592_000, "jti": secrets.token_urlsafe(16)})
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        value = await self.code_store.consume(authorization_code.code)
        if not value or value.get("client_id") != client.client_id:
            raise TokenError("invalid_grant", "The authorization code was already used.")
        return self._tokens(client.client_id, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        claims = self._verify(refresh_token, "refresh")
        if not claims or claims.get("client_id") != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            subject="personal",
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return self._tokens(client.client_id, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self.static_access_token and hmac.compare_digest(token, self.static_access_token):
            return AccessToken(
                token=token,
                client_id="steam-mcp-static-token",
                scopes=[self.scope],
                subject="personal",
                claims={"iss": self.issuer, "authentication_method": "static-bearer"},
            )
        claims = self._verify(token, "access")
        if not claims or self.scope not in str(claims.get("scope", "")).split():
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id", "")),
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            resource=self.resource,
            subject="personal",
            claims=claims,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        return None


def create_oauth_runtime(
    *,
    issuer: str,
    resource: str,
    scope: str,
    login_secret: str,
    signing_secret: str,
    access_token: str,
    store: str,
    project: str,
    collection: str,
) -> OAuthRuntime:
    if len(login_secret) < 32 or len(signing_secret) < 32:
        raise RuntimeError("OAuth login and signing secrets must be at least 32 characters")
    if store == "memory":
        code_store: AuthorizationCodeStore = MemoryAuthorizationCodeStore()
    elif store == "firestore":
        code_store = FirestoreAuthorizationCodeStore(project, collection)
    else:
        raise RuntimeError("MCP_OAUTH_STORE must be memory or firestore")
    provider = PersonalOAuthProvider(
        issuer=issuer,
        resource=resource,
        scope=scope,
        login_secret=login_secret,
        signing_secret=signing_secret,
        access_token=access_token,
        code_store=code_store,
    )
    settings = AuthSettings(
        issuer_url=AnyHttpUrl(provider.issuer),
        resource_server_url=AnyHttpUrl(provider.resource),
        required_scopes=[scope],
        client_registration_options=ClientRegistrationOptions(
            enabled=False,
            valid_scopes=[scope],
            default_scopes=[scope],
        ),
    )
    return OAuthRuntime(provider=provider, settings=settings)
