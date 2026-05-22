# Deployment playbook

Step-by-step instructions to go from `git clone` to a working Gemini Enterprise integration. For architecture background, prerequisites (org policies, IAM), and per-provider GE dialog values, see the [main README](../README.md).

## Contents

- `predeploy.sh` — lint + format + test gate; run before any deploy
- `deploy-miro.sh` — deploys the Miro MCP proxy to its own Cloud Run service
- `deploy-figma.sh` — deploys the Figma MCP proxy to its own Cloud Run service
- `deploy-lucid.sh` — deploys the Lucid MCP proxy to its own Cloud Run service

All four scripts support `--help`.

---

## 0. Operator machine prerequisites

| Tool | Why |
|---|---|
| `gcloud` ≥ 460 | Cloud Run + Artifact Registry |
| `docker` | Local image build + push |
| `python3` ≥ 3.11 with `pip` | Pre-deploy lint/format/tests |

Authenticate:

```bash
gcloud auth login                       # personal machine
gcloud auth application-default login   # for libraries using ADC
```

In CI, use a service account key with `roles/run.admin`, `roles/artifactregistry.writer`, `roles/serviceusage.serviceUsageAdmin`. **No** Secret Manager, Firestore, or IAM-policy-admin roles needed — passthrough mode doesn't use them.

## 1. Clone and install dev deps

```bash
git clone <repo-url> gemini-enterprise-custom-mcp-proxy
cd gemini-enterprise-custom-mcp-proxy
pip install -r requirements-dev.txt   # runtime + ruff + pytest + respx
```

## 2. Run the pre-deploy gate

```bash
scripts/predeploy.sh         # ruff format + ruff check + pytest (68 tests)
scripts/predeploy.sh --fix   # auto-apply lint/format fixes
```

Run this *before* deploying. If the gate is red, fix and re-run.

## 3. Confirm GCP prerequisites

These are one-time per project. Skip whichever you've already done. See the [main README → Prerequisites](../README.md#prerequisites) for full detail and gcloud commands.

- [ ] **Org policy `discoveryengine.managed.disableCustomMcpServerConnector` is `enforce: false`** at the project level. Without this, GE silently won't dispatch to your MCP server. Needs `roles/orgpolicy.policyAdmin`.
- [ ] **IAM/org policy permits `allUsers` on Cloud Run services** (either via `iam.allowedPolicyMemberDomains` override or via the per-service "Allow unauthenticated invocations" toggle in the Cloud Run UI).
- [ ] You hold `roles/discoveryengine.editor` in the GE console where you'll create the data store.

## 4. Create the SaaS app(s)

For each provider you plan to deploy, create an OAuth app and capture **client_id** + **client_secret**.

| SaaS | Console | Notes |
|---|---|---|
| Miro | https://miro.com/app/settings/user-profile/apps | Enable `boards:read`, `boards:write`, `identity:read` (and `team:*` if available) |
| Figma | https://www.figma.com/developers/apps | Enable `current_user:read`, `file_content:read`, `file_comments:read+write`, `library_content:read`, `library_assets:read` |
| Lucid | https://lucid.app/developer | Enable `user.profile`, `offline_access`, `lucidchart.document.content` |

For all three, set the **OAuth redirect URI** to:

```
https://vertexaisearch.cloud.google.com/oauth-redirect
```

(GE handles the callback — the proxy has no `/auth/*` routes in passthrough mode.)

## 5. Deploy

Only `PROJECT` is required. The proxy holds no SaaS credentials — GE owns the OAuth flow.

```bash
./scripts/deploy-miro.sh  --project <PROJECT_ID>
./scripts/deploy-figma.sh --project <PROJECT_ID>
./scripts/deploy-lucid.sh --project <PROJECT_ID>
```

Or via `.env` for repeatable customer deploys:

```bash
echo "PROJECT=<PROJECT_ID>" > customer.env
./scripts/deploy-miro.sh  --env-file customer.env
./scripts/deploy-figma.sh --env-file customer.env
./scripts/deploy-lucid.sh --env-file customer.env
```

What each script does:

1. Enables `run.googleapis.com` and `artifactregistry.googleapis.com` (nothing else)
2. Ensures the Artifact Registry repo `mcp-proxy` exists in `REGION`
3. Builds the image locally with `docker build`
4. Pushes to `${REGION}-docker.pkg.dev/${PROJECT}/mcp-proxy/{service}:{timestamp}`
5. Deploys to Cloud Run with `ACTIVE_PROVIDER={provider}` and `ALLOWED_ORIGINS=https://vertexaisearch.cloud.google.com`
6. Prints the service URL + the exact field values to paste into the GE "Custom MCP Server" dialog

Save the printed service URL for the next step.

## 6. Register each MCP server with Gemini Enterprise

```
GCP Console → Gemini Enterprise → Data stores → Create → Custom MCP Server
```

The deploy script printed the exact field values. For reference, here's the shape per provider:

### Miro

| Field | Value |
|---|---|
| MCP Server URL | `<service-url>/mcp` |
| Authorization URL | `https://miro.com/oauth/authorize` |
| Auth URL Parameters | *(blank)* |
| Token URL | `https://api.miro.com/v1/oauth/token` |
| Client ID / Secret | *(from step 4)* |
| Scopes | `boards:read boards:write identity:read` |

### Figma

| Field | Value |
|---|---|
| MCP Server URL | `<service-url>/mcp` |
| Authorization URL | `https://www.figma.com/oauth` |
| Auth URL Parameters | *(blank)* |
| Token URL | `https://api.figma.com/v1/oauth/token` |
| Client ID / Secret | *(from step 4)* |
| Scopes | `current_user:read file_comments:read file_comments:write file_content:read library_assets:read library_content:read` |

### Lucid

| Field | Value |
|---|---|
| MCP Server URL | `<service-url>/mcp` |
| Authorization URL | `https://lucid.app/oauth2/authorize` |
| Auth URL Parameters | *(blank; `offline_access` goes in Scopes)* |
| Token URL | `https://api.lucid.co/oauth2/token` |
| Client ID / Secret | *(from step 4)* |
| Scopes | `user.profile offline_access lucidchart.document.content` |

## 7. Login + Reload custom actions

For each data store:

1. **Click Login** in the dialog and complete the SaaS OAuth flow. GE stores per-user tokens.
2. Open the data store → **Actions → Reload custom actions**. GE calls `tools/list` on your `/mcp` endpoint and shows the tool catalog.
3. **Enable** the tools you want GE's agent to call.
4. **Attach** the data store to a **conversational/Agent app** (not a Search app — Search apps don't dispatch MCP tool calls).

If reload fails with 401, recheck the org-policy and Allow-unauthenticated prereqs (step 3). If it fails with 307, you're on an old branch — pull latest.

## 8. Smoke-test

Tail logs in one terminal:

```bash
gcloud run services logs tail mcp-miro --region us-central1 --project <PROJECT_ID>
```

In GE chat:

| Provider | Prompt | Expect in logs |
|---|---|---|
| Miro | "List my Miro boards" | `tool.invoke list_miro_boards` → `saas.call ...v2/boards status_code=200` |
| Figma | "Who am I in Figma?" | `tool.invoke get_figma_me` → `saas.call ...v1/me status_code=200` |
| Lucid | "Search my Lucid documents for X" | `tool.invoke search_lucid_documents` → `saas.call ...documents/search status_code=200` |

If a tool returns `Access forbidden (...)`, the SaaS access token lacks that scope — add it to GE's Scopes field and re-Login.

---

## Re-deploys later

```bash
git pull
scripts/predeploy.sh                              # validate first
./scripts/deploy-miro.sh --project <PROJECT_ID>   # or --env-file customer.env
```

Image is rebuilt + pushed, Cloud Run revision is rolled out. GE's stored per-user SaaS tokens are unaffected (they live in GE, not in the proxy) — users don't need to re-link.

## Script flags reference

```bash
./scripts/deploy-{miro,figma,lucid}.sh --help
```

| Flag | Env var | Default | Required |
|---|---|---|---|
| `--env-file <path>` | — | — | no |
| `--project` | `PROJECT` | — | **yes** |
| `--region` | `REGION` | `us-central1` | no |
| `--service-name` | `SERVICE_NAME` | `mcp-{provider}` | no |
| `--allowed-origins` | `ALLOWED_ORIGINS` | `https://vertexaisearch.cloud.google.com` | no |
| `--allow-unauthenticated` / `--no-allow-unauthenticated` | — | `--allow-unauthenticated` | no |

`predeploy.sh` flags:

| Flag | Behavior |
|---|---|
| *(none)* | Check-only; non-zero exit on any failure |
| `--fix` | Auto-apply `ruff format` + `ruff check --fix` |
| `--skip-tests` | Lint+format only, skip pytest |
| `--tests-only` | Run pytest only, skip lint/format |
