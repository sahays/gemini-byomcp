# Option A — Passthrough variant

This branch (`option-a-passthrough`) is the **stateless-passthrough** variant of the proxy, intended to be deployed side-by-side with `main` (Option B) and compared empirically against Gemini Enterprise.

## What's different from `main`

| Concern | `main` (Option B) | This branch (Option A) |
|---|---|---|
| Inbound auth | Validates Google bearer token via `oauth2.googleapis.com/tokeninfo`, pins `aud`/`azp` to `GOOGLE_CLIENT_ID` | Accepts any `Authorization: Bearer <token>` header verbatim; no validation in the proxy |
| OAuth flow with the SaaS | Proxy runs it via `/auth/{provider}` + `/auth/{provider}/callback` | **Not present.** Gemini Enterprise runs OAuth directly against `miro.com`/`figma.com`/`lucid.app` using the credentials filled in the GE "Authentication settings" dialog |
| Per-user SaaS tokens | Stored per-user-email in Firestore (LocalJsonStore in dev) | **Not stored.** GE sends each user's SaaS bearer token in the Authorization header at request time |
| Token refresh | Pre-emptive (middleware checks `expires_at`) + retry-once on 401 via `call_with_refresh` | **Not done.** A 401 from the SaaS surfaces `[ACTION REQUIRED] … re-OAuth the user` and GE handles the re-consent |
| Required env vars | `PROJECT`, `GOOGLE_CLIENT_ID`, `{PROVIDER}_CLIENT_ID`, `{PROVIDER}_CLIENT_SECRET`, `GCP_PROJECT_ID` | **`PROJECT` only.** Everything else is irrelevant in passthrough mode |
| Secret Manager | Used for SaaS client_id/secret | Not used |
| Firestore | Used for per-user tokens | Not used |
| Code paths removed | — | `/auth/*` routes, Google `tokeninfo` validation, pre-emptive refresh, DB lookup in middleware |

## How the user-experience plays out for end-users

The admin's Login during the GE "Authentication settings" dialog is a config-validation step — it lets GE confirm the Authorization URL / Token URL / client_id/secret round-trip works. Per the GE docs:

> *"User OAuth tokens ensure that the agent can only access data that the actual user has permission to see … the OAuth token is made available to the agent, allowing it to act on behalf of the user."*

So when **end-user Alice** asks GE a Miro question for the first time, GE detects there's no Miro token for Alice and walks Alice through her own OAuth consent (independently of any token the admin obtained during Login). GE stores Alice's token internally. Subsequent Miro tool calls from Alice carry Alice's token to `/mcp`. The proxy never sees or persists Alice's identity — every request is handled stateelessly against whatever bearer arrived.

## Deploy side-by-side with `main`

The two branches can coexist in the same GCP project — they deploy to differently-named Cloud Run services (default `mcp-miro` vs `mcp-miro-passthrough`).

```bash
# Option B (main) — already deployed earlier
git checkout main
./scripts/deploy-miro.sh --env-file customer.env

# Option A (this branch) — additional service in the same project
git checkout option-a-passthrough
./scripts/deploy-miro.sh --env-file customer.env     # service: mcp-miro-passthrough
```

Notes:

- The Option A deploy only needs `PROJECT` from the env file. Everything else is ignored, even if present.
- The Option A `--service-name` defaults to `mcp-miro-passthrough` so it doesn't collide with the main-branch service.

## How to configure the Option A service in Gemini Enterprise

In GE → Data stores → Create → Custom MCP Server, fill the "Authentication settings" dialog with:

| Field | Value |
|---|---|
| MCP Server URL | `https://<passthrough-service-url>/mcp` |
| Authorization URL | `https://miro.com/oauth/authorize` |
| Authorization URL Parameters | `&access_type=offline&prompt=consent` |
| Token URL | `https://api.miro.com/v1/oauth/token` |
| Client ID | your Miro app's `client_id` |
| Client Secret | your Miro app's `client_secret` |
| Scopes | space-separated subset of `boards:read boards:write boards:export identity:read projects:read projects:write` |

In the Miro developer console, set the OAuth redirect URI to `https://vertexaisearch.cloud.google.com/oauth-redirect`.

Click **Login** in the dialog. Then save.

## Expected outcomes

- **It works** end-to-end → Option A's model is correct; main branch can be slimmed down to match.
- **GE rejects with "invalid token" on first MCP call** → GE doesn't actually pass the SaaS bearer through; Option A's assumption is wrong and main's OAuth-server-style architecture is the right target.
- **Tool calls fail intermittently with 401 after ~1 hour** → SaaS access token expired and GE didn't refresh it; we'd need to check whether GE handles refresh automatically or whether we need to surface a specific re-auth signal.

Capture log lines from `gcloud run services logs read mcp-miro-passthrough --region <REGION>` if anything looks off; the proxy's structured logs will show whether the request reached `/mcp` and what token (masked) was forwarded.
