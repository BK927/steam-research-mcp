# Shared passkey login on a home server

A personal Steam MCP deployment can use the shared passkey front door in
[Cloud Browser's passkey service](https://github.com/BK927/cloud-browser-mcp/tree/main/deploy/passkey-auth)
instead of asking the operator to type an MCP OAuth login password.
Google-synced and device-bound keys are supported; the server checks the actual
WebAuthn signature and user verification.

## Integration

Keep Steam on loopback (the recorded profile uses port 8082) and preserve
the existing bearer protection, OAuth discovery, PKCE and service-specific tokens.
Add a front-door adapter with:

- A fixed loopback login URL: `http://127.0.0.1:8082/oauth/login`.
- The configured public hostname as its backend Host header.
- The existing private environment file, for example `~/.config/steam-mcp/env`,
  containing `MCP_OAUTH_LOGIN_SECRET`.
- The public login path `/steam/oauth/login`, forwarded to the corresponding
  `/login/steam` adapter.

The backend validates its original signed OAuth transaction before the front door
offers passkey verification. Only after fresh verification does the front door
submit the existing secret inside the server. The secret never goes to the browser.

Register a real key and confirm login before changing the proxy route. Preserve
`/steam/mcp`, health, token, revocation and well-known routes. Preserve the passkey
login override when redeploying; use the front-door activation/probe tools to
confirm it after any routing change. The dedicated 8443 `/oauth/login` alias must also use the same front door.

This is an optional deployment integration, not a new Steam MCP tool or
built-in multi-user authentication service. This home-server profile is separate from the Cloud Run deployment and Steam account/API credentials.

## Use and recovery

Existing connected ChatGPT apps keep their tokens. New connections or
reconnections use the registered passkey. Synced copies of the same Google
passkey do not require another registration, though a new device may need
password-manager unlocking. Google account sign-in alone is not sufficient.

Registration completion and login completion are visibly distinct in the shared
portal. Removing a synced key blocks that key across its devices and revokes its
portal sessions; previously issued MCP grants must be disconnected separately.
Keep existing SSH access and private server credentials for recovery. Linux sudo,
SSH passphrases and upstream account authentication remain separate.

On 2026-10-03 the personal home-server profile was checked for health 200,
unauthenticated MCP requests 401 and the passkey login redirect. This does not
claim that every ChatGPT app was reconnected or that other hosting profiles were changed.
