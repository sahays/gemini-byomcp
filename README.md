# Gemini Enterprise Custom MCP Proxy

A multi-provider MCP gateway that bridges **Gemini Enterprise** (GE) "Bring Your Own MCP" data stores to SaaS that don't speak MCP natively. Ships with **Miro**, **Figma**, and **Lucid** providers.

Each Cloud Run service exposes **one** provider (selected via `ACTIVE_PROVIDER`) on a single MCP endpoint:

```
https://<service-url>/mcp
```

## Architecture

- **Inbound auth**: GE sends a Google-issued bearer token. `server.py` validates it via Google's `tokeninfo` endpoint and pins the audience to `GOOGLE_CLIENT_ID`.
- **Outbound auth**: Each user links their SaaS account via a standard OAuth 2.0 authorization-code flow (`/auth/{provider}`). Tokens are stored per-user (corporate email is the key) in Firestore (prod) or a local JSON store (dev).
- **Token refresh**: The middleware pre-emptively refreshes a SaaS token whose `expires_at` is past, and `call_with_refresh()` in `providers/base.py` retries once on a 401 by refreshing inline.
- **Plugin model**: Each SaaS lives in `providers/<name>.py` implementing `BaseProvider`. Registering a new SaaS is a single file + one line in `providers/__init__.py`.

## Local development

```bash
cp .env.example .env
# fill in GOOGLE_CLIENT_ID, MIRO_* (or FIGMA_*/LUCID_*), leave GCP_PROJECT_ID blank
pip install -r requirements.txt
ACTIVE_PROVIDER=miro python server.py
```

Health check:

```bash
curl http://localhost:8080/health
# {"status":"healthy","active_provider":"miro","registry":["miro","figma","lucid"]}
```

Link a Miro account in the browser:

```
http://localhost:8080/auth/miro?user=test@example.com
```

## Cloud Run deployment (one service per provider)

> **For step-by-step instructions** from a fresh `git clone` through Gemini Enterprise registration and smoke-test, see **[`scripts/README.md`](scripts/README.md)**.

Use the per-provider scripts in `scripts/`. Each one enables the required GCP APIs, upserts the SaaS client_id/secret into Secret Manager, builds + pushes the image to Artifact Registry, deploys to Cloud Run, and updates the `*_REDIRECT_URI` env var to match the freshly minted service URL.

### Recommended customer workflow (`.env` file)

This is the easiest and most repeatable way to deploy across customer environments. The credentials are read from one local file, ingested by the script, and after the first successful deploy they live in Secret Manager — you can shred the file.

```bash
# 1. Copy the template
cp .env.example customer.env

# 2. Edit with the customer's credentials
#    - PROJECT, REGION
#    - GOOGLE_CLIENT_ID (the OAuth client Gemini Enterprise will use)
#    - MIRO_CLIENT_ID / MIRO_CLIENT_SECRET    (from the Miro developer console)
#    - FIGMA_CLIENT_ID / FIGMA_CLIENT_SECRET  (from https://www.figma.com/developers/apps)
#    - LUCID_CLIENT_ID / LUCID_CLIENT_SECRET  (from https://developer.lucid.co/)
$EDITOR customer.env

# 3. Deploy every provider
./scripts/deploy-miro.sh  --env-file customer.env
./scripts/deploy-figma.sh --env-file customer.env
./scripts/deploy-lucid.sh --env-file customer.env

# 4. Secrets now live in Secret Manager. Shred the file so they aren't on disk.
shred -u customer.env   # or: rm -P customer.env on macOS
```

Each script:
- Prints a masked config banner so you can verify which values are in play before any GCP calls
- Fails fast (non-zero exit) if any required value is missing, before touching gcloud
- Uploads the secrets to GCP Secret Manager (creates the secret on first run, adds a new version thereafter)
- Prints the SaaS-side redirect URI and the MCP URL to register in Gemini Enterprise at the end

### Alternative — pass values directly on the command line

Useful for CI/CD where the secrets come from a vault and are exported as env vars:

```bash
export PROJECT=my-gcp-project
export GOOGLE_CLIENT_ID=1234.apps.googleusercontent.com
export MIRO_CLIENT_ID=...
export MIRO_CLIENT_SECRET=...
./scripts/deploy-miro.sh
```

Or with explicit flags (avoid for secrets — they'll land in shell history):

```bash
./scripts/deploy-miro.sh \
  --project my-gcp-project \
  --google-client-id 1234.apps.googleusercontent.com \
  --miro-client-id <miro_client_id> \
  --miro-client-secret <miro_client_secret>
```

### Defaults and overrides

`--region us-central1`, `--service-name mcp-{provider}`, `--allowed-origins https://vertexaisearch.cloud.google.com`, `--allow-unauthenticated` (use `--no-allow-unauthenticated` to require IAM auth at the network layer; the proxy still enforces Google token validation either way).

Each Cloud Run service receives **only its own provider's** env vars/secrets — the Figma deployment never sees Miro or Lucid credentials, and so on.

## Registering with Gemini Enterprise

1. **MCP server URL** → `https://<service-url>/mcp` (StreamableHTTP transport)
2. **Authorization URL** → your IdP's OAuth authorize endpoint (the IdP that issues the bearer token GE sends to this proxy)
3. **Token URL** → your IdP's token endpoint
4. **OAuth redirect URL** (registered with your IdP) → `https://vertexaisearch.cloud.google.com/oauth-redirect`
5. **Scopes** → at minimum include `openid email`; add `offline_access` for refresh tokens

After registration, in the GE data-store UI click **Actions → Reload custom actions** to populate tool definitions.

## APIs and scopes by SaaS

Scope strings are defined once per provider as `SCOPE_*` constants in `providers/{name}.py:ALL_SCOPES`. The consent URL requests every entry in `ALL_SCOPES` so the granted token is broad enough to back any future tool without re-prompting the user. Each `@require_scopes(...)` decorator references the constants — never bare strings — so the consent URL and tool requirements can't drift.

We expose every non-admin scope the three SaaS document. Excluded scopes are anything that manages users, organizations, audit/compliance, or sessions (i.e. anything an org admin would gate).

### Inbound (Google) — applies to every provider

| Method | URL | Purpose |
|---|---|---|
| GET | `https://oauth2.googleapis.com/tokeninfo?access_token={t}` | Validate Gemini Enterprise's bearer token; extract `aud`/`azp`/`email` |

### Miro

| Method | URL | Tool | Required scope |
|---|---|---|---|
| GET | `https://miro.com/oauth/authorize?...&scope=<ALL_SCOPES>` | OAuth consent | — |
| POST | `https://api.miro.com/v1/oauth/token` *(query string)* | code exchange & refresh | — |
| GET | `https://api.miro.com/v2/boards/{board_id}/items?limit=50` | `get_miro_board_items` | `boards:read` |
| POST | `https://api.miro.com/v2/boards/{board_id}/items` | `create_miro_sticky_note` | `boards:write` |
| DELETE | `https://api.miro.com/v2/boards/{board_id}/items/{item_id}` | `delete_miro_board_item` | `boards:write` |

**Miro scopes requested (6):** `boards:read`, `boards:write`, `boards:export`, `identity:read`, `projects:read`, `projects:write`.

### Figma

| Method | URL | Tool | Required scope |
|---|---|---|---|
| GET | `https://www.figma.com/oauth?...&scope=<ALL_SCOPES>` | OAuth consent | — |
| POST | `https://api.figma.com/v1/oauth/token` *(Basic auth)* | code exchange | — |
| POST | `https://api.figma.com/v1/oauth/refresh` *(Basic auth)* | refresh | — |
| GET | `https://api.figma.com/v1/files/{file_key}?depth=1` | `get_figma_file` | `file_content:read` |
| GET | `https://api.figma.com/v1/files/{file_key}/nodes?ids=...` | `get_figma_file_nodes` | `file_content:read` |
| GET | `https://api.figma.com/v1/teams/{team_id}/components?page_size=30` | `get_figma_team_components` | `library_content:read` |

**Figma scopes requested (19):** `current_user:read`, `file_content:read`, `file_metadata:read`, `file_comments:read`, `file_comments:write`, `file_dev_resources:read`, `file_dev_resources:write`, `file_variables:read` *(Enterprise)*, `file_variables:write` *(Enterprise)*, `file_versions:read`, `library_content:read`, `library_assets:read`, `library_analytics:read` *(Enterprise)*, `team_library_content:read`, `selections:read`, `projects:read` *(private OAuth apps only)*, `project_metadata:read`, `webhooks:read`, `webhooks:write`.

### Lucid

| Method | URL | Tool | Required scope |
|---|---|---|---|
| GET | `https://lucid.app/oauth2/authorize?...&scope=<ALL_SCOPES>` | OAuth consent | — |
| POST | `https://api.lucid.co/oauth2/token` *(JSON body)* | code exchange & refresh | — |
| GET | `https://api.lucid.co/users/me` | `get_lucid_user_profile` | `user.profile` |
| GET | `https://api.lucid.co/documents/{document_id}/contents` | `get_lucid_document_contents` | `lucidchart.document.content` |
| POST | `https://api.lucid.co/documents/search` | `search_lucid_documents` | `lucidchart.document.content` |

**Lucid scopes requested (9, parent scopes only — Lucid grants child permissions automatically):** `user.profile`, `offline_access`, `folder`, `lucidchart.document.content`, `lucidchart.document.app`, `lucidspark.document.content`, `lucidspark.document.app`, `lucidscale.document.content`, `lucidscale.document.app`.

### When a scope check fails

If a tool is invoked but the user's stored token lacks the scope, the proxy returns a single-line message:

```
[PERMISSION REQUIRED] Miro scope 'boards:write' not granted.
```

If the user has no token at all, it returns the `[ACTION REQUIRED]` re-link prompt with the `/auth/{provider}?user=…` URL.

## Pre-deploy checks (lint + format + tests)

`scripts/predeploy.sh` is the gate to run before any deploy. It runs `ruff format --check`, `ruff check`, and the full `pytest` suite against the Python sources in `config.py`, `database.py`, `server.py`, `providers/`, and `tests/`. Configuration lives in `pyproject.toml`.

```bash
pip install -r requirements-dev.txt

scripts/predeploy.sh                # check-only; non-zero exit on any failure
scripts/predeploy.sh --fix          # auto-apply ruff format + lint fixes
scripts/predeploy.sh --skip-tests   # fast loop during dev (lint + format only)
scripts/predeploy.sh --tests-only   # CI re-run after a fix commit
```

Recommended flow:

```bash
scripts/predeploy.sh --fix          # make the tree clean
scripts/predeploy.sh                # confirm it stays clean
./scripts/deploy-miro.sh            # deploy
```

## Running the test suite

```bash
pip install -r requirements-dev.txt
pytest -v
```

The suite covers:

| File | What it asserts |
|---|---|
| `tests/test_security.py` | WIF middleware: missing/invalid/aud-mismatched/email-less Google tokens are rejected; `/health` and `/auth/*` bypass auth |
| `tests/test_scopes.py` | `require_scopes` raises `MissingScopeException`; `auth_boundary` translates it into `[ACTION REQUIRED]` / `[PERMISSION REQUIRED]` links with the right URL scheme |
| `tests/test_refresh.py` | `call_with_refresh` retries once on 401, persists rotated tokens, preserves the old refresh_token when the provider omits it, and surfaces `RefreshFailedException` correctly; `compute_expires_at` / `is_expired` math |
| `tests/test_database.py` | `LocalJsonStore` round-trip incl. `expires_at`, per-user and per-provider isolation, namespaced JSON keys |
| `tests/test_miro.py` | Authorize URL shape; token exchange + refresh use the right params on `api.miro.com/v1/oauth/token`; the 3 tools hit the right `api.miro.com/v2/...` endpoints with `Bearer`; write tools require `boards:write` |
| `tests/test_figma.py` | Authorize URL includes default scopes; token exchange uses `Authorization: Basic`; **refresh hits `/v1/oauth/refresh`** (not `/oauth/token`); tools call `/v1/files/...`, `/v1/files/.../nodes`, `/v1/teams/.../components` with `Bearer` |
| `tests/test_lucid.py` | Authorize URL includes `offline_access`; token exchange + refresh post JSON body to `api.lucid.co/oauth2/token`; tools send `Lucid-Api-Version: 1`; `get_lucid_document_contents` sends `Accept: application/vnd.lucid.contents+json` |

All downstream HTTP traffic is mocked with `respx`, so the suite never touches real Miro/Figma/Lucid/Google endpoints.

## Verifying refresh-token rotation

```bash
# Force expiry locally, then call a tool. Look for "Pre-emptively refreshed ... token" in logs.
python -c "import json; d=json.load(open('default.json')); \
  d['test@example.com']['miro_expires_at']='2000-01-01T00:00:00+00:00'; \
  json.dump(d, open('default.json','w'), indent=2)"
```

## References

- [GE Custom MCP setup](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server)
- [GE MCP server descriptions](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/writing-mcp-server-descriptions)
- [Figma OAuth](https://developers.figma.com/docs/rest-api/oauth-apps/)
- [Lucid OAuth](https://developer.lucid.co/reference/authorization-endpoints)
- [Miro OAuth](https://developers.miro.com/docs/getting-started-with-oauth)
