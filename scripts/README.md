# Deployment playbook

Step-by-step instructions to go from `git clone` to a working Gemini Enterprise integration. For background on the architecture, see the [main README](../README.md).

## Contents

- `predeploy.sh` — lint + format + test gate; run before any deploy
- `deploy-miro.sh` — deploys the Miro MCP proxy to its own Cloud Run service
- `deploy-figma.sh` — deploys the Figma MCP proxy to its own Cloud Run service
- `deploy-lucid.sh` — deploys the Lucid MCP proxy to its own Cloud Run service

All four scripts support `--help`.

---

## 0. Prerequisites on the operator machine

| Tool | Why |
|---|---|
| `gcloud` ≥ 460 | Cloud Run, Secret Manager, Artifact Registry |
| `docker` (any modern version) | Local image build/push to Artifact Registry |
| `python3` ≥ 3.11 with `pip` | Pre-deploy lint/format/tests |

Authenticate:

```bash
gcloud auth login                       # browser flow on personal machines
gcloud auth application-default login   # for libraries that use ADC
```

In CI, use a service account key bound to the customer's project with roles `roles/run.admin`, `roles/iam.serviceAccountUser`, `roles/artifactregistry.writer`, `roles/secretmanager.admin`, `roles/datastore.user`.

## 1. Clone and install dev deps

```bash
git clone <repo-url> gemini-enterprise-custom-mcp-proxy
cd gemini-enterprise-custom-mcp-proxy

pip install -r requirements-dev.txt   # runtime + ruff + pytest + respx
```

## 2. Run the pre-deploy gate

```bash
scripts/predeploy.sh        # ruff format + ruff check + pytest (55 tests)
```

Run this *before* touching the customer's GCP project. If anything is red:

```bash
scripts/predeploy.sh --fix  # apply formatting and auto-fixable lint
scripts/predeploy.sh        # re-verify
```

## 3. Create OAuth apps in the three SaaS

For each, create an app in the developer console and capture the **client_id** + **client_secret**. You can use a placeholder for the redirect URI now — you'll fill in the real one in step 7.

| SaaS | Developer console | Notes |
|---|---|---|
| Miro | https://miro.com/app/settings/user-profile/apps | Enable the scopes you want; see [main README](../README.md#apis-and-scopes-by-saas) for the full non-admin list |
| Figma | https://www.figma.com/developers/apps | `file_variables:*` and `library_analytics:read` require Enterprise; `projects:read` requires a private OAuth app |
| Lucid | https://developer.lucid.co/ | Granting parent scopes (`lucidchart.document.content` etc.) covers child permissions automatically |

## 4. Create the Google OAuth client (for Gemini Enterprise inbound)

This client lets Gemini Enterprise issue bearer tokens that the proxy will validate via Google's `tokeninfo` endpoint.

```
GCP Console → APIs & Services → Credentials → Create Credentials → OAuth client ID
  Application type:        Web application
  Authorized redirect URI: https://vertexaisearch.cloud.google.com/oauth-redirect
```

Capture the **client ID**. You'll set it as `GOOGLE_CLIENT_ID` next.

## 5. Build the credentials file

```bash
cp .env.example customer.env
$EDITOR customer.env
```

Fill in:

```dotenv
PROJECT=customer-gcp-project-id
REGION=us-central1
GOOGLE_CLIENT_ID=<from step 4>

MIRO_CLIENT_ID=<from step 3>
MIRO_CLIENT_SECRET=<from step 3>

FIGMA_CLIENT_ID=<from step 3>
FIGMA_CLIENT_SECRET=<from step 3>

LUCID_CLIENT_ID=<from step 3>
LUCID_CLIENT_SECRET=<from step 3>
```

## 6. Deploy each provider

```bash
./scripts/deploy-miro.sh  --env-file customer.env
./scripts/deploy-figma.sh --env-file customer.env
./scripts/deploy-lucid.sh --env-file customer.env
```

Per provider, each script:

1. Enables `run`, `artifactregistry`, `secretmanager`, `firestore` APIs
2. Upserts the SaaS client_id/secret into Secret Manager (`{provider}-client-id`, `{provider}-client-secret`)
3. Creates the Artifact Registry repo `mcp-proxy` if missing
4. Builds the image locally with `docker build` and pushes to `${REGION}-docker.pkg.dev/${PROJECT}/mcp-proxy/{service-name}:{timestamp}`
5. Deploys to Cloud Run with `ACTIVE_PROVIDER={provider}`, secrets bound via `--set-secrets`, env vars via `--set-env-vars`
6. Reads back the resulting service URL and updates the `{PROVIDER}_REDIRECT_URI` env var to `https://<service-url>/auth/{provider}/callback`

Each script ends with a banner that prints:

```
Service URL:   https://mcp-miro-abc123-uc.a.run.app
MCP endpoint:  https://mcp-miro-abc123-uc.a.run.app/mcp
Next steps:
  1. In the Miro developer console, set the OAuth redirect URI to:
       https://mcp-miro-abc123-uc.a.run.app/auth/miro/callback
  2. In Gemini Enterprise, register the MCP server URL: ...
```

**Save those URLs.**

## 7. Register each SaaS's redirect URI

Go back to each SaaS developer console (step 3) and set the OAuth redirect URI to the one printed by the deploy script (`https://mcp-{provider}-…/auth/{provider}/callback`). Without this, the OAuth callback in step 10 will fail.

## 8. Register each MCP server with Gemini Enterprise

```
GCP Console → Gemini Enterprise → Data stores → Create → Custom MCP Server
  MCP server URL:        https://mcp-{provider}-…/mcp
  Authorization URL:     <your IdP's OAuth authorize endpoint>
  Token URL:             <your IdP's OAuth token endpoint>
  Client ID / Secret:    from step 4
  Scopes:                openid email offline_access
```

Repeat for `mcp-miro`, `mcp-figma`, `mcp-lucid`. Then click **Actions → Reload custom actions** so GE pulls the tool definitions.

## 9. Shred the credentials file

The secrets are now in Secret Manager. Don't leave the file on disk.

```bash
shred -u customer.env      # macOS: rm -P customer.env
```

## 10. Smoke-test

In Gemini Enterprise, ask a question that exercises one tool per provider — e.g.

> "List components in my Figma team `<team_id>`"

On the first invocation, the proxy returns:

```
[ACTION REQUIRED] Your Figma account is not linked.
Please link your account securely by visiting this authorization link:
https://mcp-figma-…/auth/figma?user=alice@corp.example
```

The user clicks the link, completes the Figma OAuth consent screen, then re-asks the question and the tool returns data.

If a tool returns:

```
[PERMISSION REQUIRED] Figma scope 'library_content:read' not granted.
```

then that scope wasn't included in the SaaS app's enabled scopes (step 3) — fix it in the SaaS developer console and have the user re-link.

---

## Re-deploys later

```bash
git pull
scripts/predeploy.sh                              # validate first
./scripts/deploy-miro.sh --env-file customer.env  # or export the env vars from a vault
```

The image is rebuilt + pushed locally, secrets in Secret Manager are reused, Cloud Run is updated in place. Users keep their existing token grants — their refresh_tokens live in Firestore and the new container picks them up immediately.

## Script flags reference

All three deploy scripts accept the same shape. See `scripts/deploy-{miro,figma,lucid}.sh --help` for the authoritative list.

| Flag | Equivalent env var | Default | Required |
|---|---|---|---|
| `--env-file <path>` | — | — | no |
| `--project` | `PROJECT` | — | yes |
| `--google-client-id` | `GOOGLE_CLIENT_ID` | — | yes |
| `--{provider}-client-id` | `{PROVIDER}_CLIENT_ID` | — | yes |
| `--{provider}-client-secret` | `{PROVIDER}_CLIENT_SECRET` | — | yes |
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
