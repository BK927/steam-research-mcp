# Steam Research MCP Server on Google Cloud Run

This is the managed Google Cloud profile for Steam Research MCP Server 2.2.0. It preserves local stdio but uses managed Google Cloud state for remote work. No deployment was executed from this repository as part of the refactor itself.

## Fixed v1 topology

| Resource | Name/default | Region and capacity | Exposure |
| --- | --- | --- | --- |
| Artifact Registry | `mcp/steam-mcp` | `asia-northeast1` | private |
| MCP Cloud Run service | `steam-mcp` | 1 vCPU, 512 MiB, concurrency 10, timeout 300 s, min 0, max 1 | public ingress; bearer or personal OAuth access token required at `/mcp` |
| Worker Cloud Run service | `steam-mcp-worker` | 1 vCPU, 1 GiB, concurrency 1, timeout 1,800 s, min 0, max 2 | private IAM |
| Cloud Tasks queue | `steam-mcp-jobs` | `asia-northeast1`; 1 dispatch/s, 2 concurrent, 3 attempts | runtime identity can enqueue |
| Firestore | `(default)` / `steam_jobs` | Native mode, `asia-northeast1` | runtime and worker identities |
| Cloud Storage | `PROJECT-steam-mcp-jobs` | `asia-northeast1`, uniform access, public access prevention | runtime read; worker object admin |

The container image is identical for the MCP and worker services. `STEAM_PROCESS_ROLE=mcp|worker` selects the entrypoint behavior. The Cloud Run profile exposes `/health` for liveness; local defaults remain `/healthz`. The worker also exposes `POST /internal/jobs/run`; it does not expose `/mcp`.

Firestore stores job state and an `expires_at` Timestamp. Its TTL policy removes expired job documents asynchronously. Cloud Storage deletes result objects when their age reaches seven days. The application sets cloud job expiry to 604,800 seconds; TTL deletion is not an immediate scheduling guarantee.

## Identities and least privilege

| Identity | Required access |
| --- | --- |
| `steam-mcp-runner` | Firestore `datastore.user`, Cloud Tasks enqueuer, bucket object viewer, act-as on `steam-mcp-tasks`, access only to the MCP/worker/cursor/optional Steam secrets it consumes |
| `steam-mcp-worker` | Firestore `datastore.user`, bucket object admin, access only to worker/cursor/optional Steam secrets |
| `steam-mcp-tasks` | Cloud Run invoker on `steam-mcp-worker` only |
| Cloud Build default identity | Artifact Registry writer on repository `mcp` only |

Cloud Run IAM plus the Cloud Tasks OIDC token is the worker authorization boundary. `STEAM_JOB_WORKER_TOKEN` adds a second private header check; it is not a substitute for IAM. The OIDC audience is the exact worker URL including `/internal/jobs/run`, registered as a Cloud Run custom audience.

## Prerequisites

- PowerShell 7 (`pwsh`), Git, `uv`, and a current Google Cloud CLI.
- A billed Google Cloud project.
- Operator permission to enable APIs, create IAM/service accounts, Firestore, Storage, Cloud Tasks, Artifact Registry, Secret Manager, Cloud Build, and Cloud Run resources.
- A clean Git worktree. Deployment refuses dirty state because an image tagged with a commit SHA must represent that exact source tree.

## One-time provisioning

```powershell
pwsh -File .\scripts\provision-gcp.ps1 `
  -ProjectId "YOUR_PROJECT_ID" `
  -Region "asia-northeast1"
```

Optionally initialize or replace the Steam Web API key:

```powershell
pwsh -File .\scripts\provision-gcp.ps1 `
  -ProjectId "YOUR_PROJECT_ID" `
  -ConfigureSteamApiKey
```

Provisioning is idempotent for infrastructure and does not rotate an existing MCP bearer, worker token, cursor secret, or personal OAuth login/signing secret. It creates an initial random value only when the corresponding secret has no enabled version. The cursor secret is independently pinned to both revisions so restarts and two workers can verify the same opaque cursors. If `(default)` Firestore already exists outside `asia-northeast1`, the script stops instead of silently creating a cross-region design.

## Candidate deployment and promotion

```powershell
pwsh -File .\scripts\deploy-cloud-run.ps1 `
  -ProjectId "YOUR_PROJECT_ID" `
  -Region "asia-northeast1" `
  -Promote
```

The script performs this sequence:

1. Require a clean Git commit and build `steam-mcp:GIT_SHA` with Cloud Build.
2. Resolve `sha256:...` from Artifact Registry and deploy only `IMAGE@DIGEST`.
3. Create a private worker `candidate` revision with `--no-traffic`.
4. Grant only `steam-mcp-tasks` the worker invoker role.
5. Create a public MCP `candidate` revision with `--no-traffic` and exact stable/candidate Host allowlists.
6. Smoke candidate `/health`, OAuth authorization/resource discovery, and unauthenticated rejection, then use the pinned MCP v2 client to negotiate protocol/SSE, require the exact eight-tool list with no legacy names, and complete a representative keyless `steam_game_get` call against the candidate URL.
7. With `-Promote`, move the worker to 100% first and the MCP service to 100% second. Without it, both final candidates stay at 0%.

On the first deployment there is no old revision to retain traffic. Cloud Run must create zero-traffic bootstrap revisions to discover stable service and tag URLs. The script requires `-Promote`, keeps bearer/IAM protection enabled, immediately replaces those bootstraps with hardened zero-traffic candidates from the same digest, smokes them, and promotes them in the same run.

Access and optional API secrets are referenced as concrete numeric versions. Existing bearer tokens are reused. Rotate only when explicitly requested:

```powershell
pwsh -File .\scripts\deploy-cloud-run.ps1 `
  -ProjectId "YOUR_PROJECT_ID" `
  -RotateAccessToken `
  -Promote
```

Rotation creates a new numeric version. The local Windows user variable `STEAM_MCP_ACCESS_TOKEN` is updated only after `-Promote`, so a zero-traffic candidate cannot silently replace the credential for the still-serving revision. Old secret versions remain available for deliberate rollback until the operator disables them.

## Verification

The deploy script already checks the candidate. After promotion:

```powershell
$baseUrl = "https://YOUR_STEAM_SERVICE.run.app"
Invoke-RestMethod "$baseUrl/health"

$headers = @{
  Authorization = "Bearer $env:STEAM_MCP_ACCESS_TOKEN"
  Accept = "application/json, text/event-stream"
  "MCP-Protocol-Version" = "2026-07-28"
}
$body = '{"jsonrpc":"2.0","id":"verify","method":"tools/list","params":{}}'
Invoke-WebRequest "$baseUrl/mcp" -Method Post -Headers $headers -ContentType application/json -Body $body
```

The request body limit is 2 MiB. Tool results default to 12 KiB and cannot exceed 32 KiB.

For ChatGPT developer-mode registration, use the production `/mcp` URL and choose OAuth. The authorization page asks for the personal key. Copy it without printing it:

```powershell
pwsh -File .\scripts\copy-chatgpt-oauth-key.ps1 -ProjectId "YOUR_PROJECT_ID"
```

## Rollback

List ready revisions and their images/secrets before selecting an exact pair:

```powershell
gcloud run revisions list --service steam-mcp --region asia-northeast1 --project YOUR_PROJECT_ID
gcloud run revisions list --service steam-mcp-worker --region asia-northeast1 --project YOUR_PROJECT_ID
```

Then roll the public service back first so it stops issuing new-format jobs, followed by its matching worker:

```powershell
pwsh -File .\scripts\rollback-cloud-run.ps1 `
  -ProjectId "YOUR_PROJECT_ID" `
  -McpRevision "KNOWN_GOOD_MCP_REVISION" `
  -WorkerRevision "MATCHING_WORKER_REVISION"
```

Traffic changes do not rebuild the image. Because secret references are revision-scoped numeric versions, a rollback also restores the prior revision's configured secret versions.

## Operations, security, and cost

- Main and worker scale to zero. Firestore, Cloud Storage, Cloud Tasks, Artifact Registry retention, Cloud Build, logging, and outbound Steam traffic can still incur charges.
- Maximum one MCP instance avoids gratuitous cache duplication; correctness does not rely on its memory because cloud job state is external. The worker can scale to two, matching queue concurrency.
- Logs must never include bearer/API key values, Cloud Tasks authorization headers, cursor secrets, or raw untrusted review bodies.
- Alert on Cloud Run 5xx/latency, Cloud Tasks oldest task age and retry exhaustion, Firestore errors, bucket growth, and Steam upstream throttling.
- Keep the API key restrictions, bucket public-access prevention, Firestore delete protection, and service-specific secret IAM under drift review.
- The fixed bearer and personal OAuth flow are suitable for a private operator deployment. A shared external product needs a separate multi-user authorization, audit, quota, and revocation design.

## Reproducibility boundary

The Python base image is pinned by patch tag and a verified multi-platform index digest. `requirements.lock` contains the complete universal runtime/build dependency graph with exact versions and distribution hashes; Docker and CI both install it with `--require-hashes`. The project is then installed with `--no-build-isolation --no-deps`, so the pinned Hatchling and runtime graph are reused instead of resolving during a build. `requirements-dev.lock` separately fixes the test/lint graph so it does not enlarge the production image.

The Debian `ca-certificates` package installed with `apt` is not snapshot-pinned, so rebuilding an old commit can still receive a later Debian security revision. This boundary is documented instead of claiming a bit-for-bit image rebuild.

Regenerate and review the locks explicitly when dependencies change:

```powershell
uv pip compile pyproject.toml requirements-build.in --extra gcp --universal --python-version 3.10 --generate-hashes --output-file requirements.lock
uv pip compile requirements-dev.in --universal --python-version 3.10 --generate-hashes --output-file requirements-dev.lock
```
