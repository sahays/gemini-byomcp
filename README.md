# Gemini Enterprise Custom MCP Proxy — passthrough variant

A multi-provider MCP proxy that connects **Gemini Enterprise** (GE) "Bring Your Own MCP" data stores to SaaS that don't speak MCP natively. Ships with **Miro**, **Figma**, and **Lucid**. One Cloud Run service per provider (selected via `ACTIVE_PROVIDER`).

```
GE chat → POST /mcp  (Bearer = SaaS access token, minted by GE's OAuth flow)
                → proxy forwards the bearer to api.miro.com / api.figma.com / api.lucid.co
                → response streams back to GE → user
```

GE owns the OAuth flow with the SaaS using the client id/secret you paste into the GE "Update authentication" dialog. The proxy is **stateless** — no Firestore, no Secret Manager, no `/auth/*` routes, no per-user token storage.

## Prerequisites

You need these set up **before** deploying:

### 1. Organization Policy — override `Disable custom mcp server connector` *(critical)*

Gemini Enterprise ships with this org constraint **enforced** by default. With it enforced, GE silently fails to dispatch to your MCP server even though the data store appears Active.

Needs `roles/orgpolicy.policyAdmin`. Override at the project (or org) level:

```bash
cat > /tmp/policy.yaml <<EOF
name: projects/<PROJECT_ID>/policies/discoveryengine.managed.disableCustomMcpServerConnector
spec:
  rules:
    - enforce: false
EOF
gcloud org-policies set-policy /tmp/policy.yaml
```

Verify:

```bash
gcloud org-policies describe \
  discoveryengine.managed.disableCustomMcpServerConnector \
  --project=<PROJECT_ID> --effective
# spec.rules[0].enforce should be: false
```

### 2. Organization Policy — `iam.allowedPolicyMemberDomains` *(may need override)*

If your org restricts which principals can appear in IAM policies, the deploy script's `--allow-unauthenticated` won't be applied automatically. The proxy still gates access at the application layer (bearer required), but GE's call gets 401'd at the Cloud Run network layer before reaching the app.

Two options:
- Toggle **Allow unauthenticated invocations** in the Cloud Run console UI for each service (often works even when the CLI binding doesn't), **or**
- Override the policy at this project to allow `allUsers`:
  ```bash
  cat > /tmp/allow-public.yaml <<EOF
  name: projects/<PROJECT_ID>/policies/iam.allowedPolicyMemberDomains
  spec:
    rules:
      - allowAll: true
  EOF
  gcloud org-policies set-policy /tmp/allow-public.yaml
  ```

### 3. Caller IAM

The user running the deploy script needs:

- `roles/run.admin` — deploy / update Cloud Run services
- `roles/artifactregistry.writer` — push images
- `roles/serviceusage.serviceUsageAdmin` — enable APIs

The user configuring the data store in the GE console needs:

- `roles/discoveryengine.editor` — add custom MCP data stores in the GE UI

### 4. The SaaS app *(one per provider)*

| Provider | Where | What you'll need |
|---|---|---|
| Miro | https://miro.com/app/settings/user-profile/apps | Client ID + Client Secret, scopes enabled per provider section below |
| Figma | https://www.figma.com/developers/apps | Client ID + Client Secret, scopes enabled |
| Lucid | https://lucid.app/developer | Client ID + Client Secret, scopes enabled |

For each app, set the **OAuth redirect URI** to:

```
https://vertexaisearch.cloud.google.com/oauth-redirect
```

GE — not the proxy — handles the OAuth callback. The Cloud Run service URL goes in GE's "MCP Server URL" field, not in the SaaS app config.

## Deploy

> **For step-by-step walkthrough** including GE registration, see [`scripts/README.md`](scripts/README.md).

```bash
# Only PROJECT is required; everything else has sensible defaults.
./scripts/deploy-miro.sh  --project <PROJECT_ID>
./scripts/deploy-figma.sh --project <PROJECT_ID>
./scripts/deploy-lucid.sh --project <PROJECT_ID>
```

Each script:
- Enables `run.googleapis.com` and `artifactregistry.googleapis.com` only
- Builds the image locally, pushes to Artifact Registry, deploys to Cloud Run
- Prints the MCP endpoint URL and the exact values to paste into the GE "Authentication settings" dialog

No Firestore, no Secret Manager, no IAM role grants — passthrough proxy genuinely needs none.

## Tools and scopes

Scopes are defined as `SCOPE_*` constants in `providers/<name>.py:ALL_SCOPES`. The recommended scope strings printed by the deploy scripts are the **minimum viable** subset — you can paste more into GE's Scopes field if you want broader tool coverage.

### Miro (10 tools)

| Tool | Scope |
|---|---|
| `list_miro_boards` *(zero-arg)*, `search_miro_boards`, `get_miro_board`, `get_miro_board_items` | `boards:read` |
| `create_miro_board`, `create_miro_sticky_note`, `delete_miro_board_item` | `boards:write` |
| `get_miro_token_info` | `identity:read` |
| `list_miro_teams` | `team:read` |
| `create_miro_team` | `team:write` |

**Recommended GE scope string:** `boards:read boards:write identity:read`

### Figma (16 tools)

| Tool | Scope |
|---|---|
| `get_figma_me` *(zero-arg)* | `current_user:read` |
| `get_figma_file`, `get_figma_file_nodes`, `render_figma_file_images`, `get_figma_file_image_fills` | `file_content:read` |
| `get_figma_file_metadata` | `file_metadata:read` |
| `get_figma_file_comments` | `file_comments:read` |
| `post_figma_file_comment` | `file_comments:write` |
| `get_figma_file_versions` | `file_versions:read` |
| `get_figma_file_dev_resources` | `file_dev_resources:read` |
| `get_figma_file_components`, `get_figma_file_styles` | `library_content:read` |
| `get_figma_component`, `get_figma_style` | `library_assets:read` |
| `get_figma_team_components`, `get_figma_team_styles` | `team_library_content:read` |

**Recommended GE scope string:** `current_user:read file_comments:read file_comments:write file_content:read library_assets:read library_content:read`

### Lucid (3 tools)

| Tool | Scope |
|---|---|
| `get_lucid_user_profile` | `user.profile` |
| `get_lucid_document_contents` | `lucidchart.document.content` |
| `search_lucid_documents` | `lucidchart.document.content` |

**Recommended GE scope string:** `user.profile offline_access lucidchart.document.content`

(`offline_access` is required for refresh tokens. Lucid grants child permissions automatically when the `lucidchart.document.content` parent is granted.)

## Step-by-step: register each SaaS in GE

Same shape for all three. Substitute `<service-url>` with what the deploy script printed.

### Miro

1. **Configure the Miro app** at https://miro.com/app/settings/user-profile/apps:
   - **Redirect URI**: `https://vertexaisearch.cloud.google.com/oauth-redirect`
   - **Enable scopes**: `boards:read`, `boards:write`, `identity:read` (and `team:read` / `team:write` if you want team tools — Enterprise plans only)
   - Copy **Client ID** and **Client Secret**.

2. **Deploy the proxy** (skip if already deployed):
   ```bash
   ./scripts/deploy-miro.sh --project <PROJECT_ID>
   ```
   Note the `Service URL` from the output.

3. **Register the data store in GE Console → Data stores → Create → Custom MCP Server**:
   | Field | Value |
   |---|---|
   | MCP Server URL | `<service-url>/mcp` |
   | Authorization URL | `https://miro.com/oauth/authorize` |
   | Authorization URL Parameters | *(leave blank)* |
   | Token URL | `https://api.miro.com/v1/oauth/token` |
   | Client ID | *(your Miro app's client_id)* |
   | Client Secret | *(your Miro app's client secret)* |
   | Scopes | `boards:read boards:write identity:read` |

4. **Click Login** in the dialog and complete Miro consent. The page should bounce you back to GE with a green "Connected" indicator.

5. **Reload custom actions**: open the data store → **Actions → Reload custom actions**. GE calls `tools/list` against `/mcp` and populates the catalog. (If this fails with 401, recheck Prerequisites #1 and #2.)

6. **Enable tools**: in the actions list, toggle on the tools you want GE's agent to be able to call. Start with `list_miro_boards`, `get_miro_token_info`, `get_miro_board_items` to validate.

7. **Attach to an App**: GE Apps → your conversational app → Data sources → add the Miro data store. (A pure Search app won't dispatch MCP tool calls.)

8. **Test**: in GE chat, ask *"List my Miro boards"*. You should see real board names. Tail logs with `gcloud run services logs tail mcp-miro --region us-central1 --project <PROJECT_ID>` and confirm `http.ingress` → `tool.invoke list_miro_boards` → `saas.call ... status_code=200` events.

### Figma

1. **Configure the Figma app** at https://www.figma.com/developers/apps:
   - **Redirect URI**: `https://vertexaisearch.cloud.google.com/oauth-redirect`
   - **Enable scopes**: `current_user:read`, `file_content:read`, `file_comments:read`, `file_comments:write`, `library_assets:read`, `library_content:read` (add more later for the Enterprise-tier tools)
   - Copy **Client ID** and **Client Secret**.

2. **Deploy**:
   ```bash
   ./scripts/deploy-figma.sh --project <PROJECT_ID>
   ```

3. **Register in GE → Data stores → Create → Custom MCP Server**:
   | Field | Value |
   |---|---|
   | MCP Server URL | `<service-url>/mcp` |
   | Authorization URL | `https://www.figma.com/oauth` |
   | Authorization URL Parameters | *(leave blank)* |
   | Token URL | `https://api.figma.com/v1/oauth/token` |
   | Client ID | *(your Figma app's client_id)* |
   | Client Secret | *(your Figma app's client secret)* |
   | Scopes | `current_user:read file_comments:read file_comments:write file_content:read library_assets:read library_content:read` |

4. **Click Login** and complete Figma consent.

5. **Reload custom actions** on the data store.

6. **Enable tools**: start with `get_figma_me`, `get_figma_file`, `get_figma_file_comments`.

7. **Attach to an App** (same as Miro step 7).

8. **Test**: ask GE *"Who am I in Figma?"* — should return your Figma handle and email. Then *"What's in Figma file `<file_key>`?"* for a real file query.

### Lucid

1. **Configure the Lucid app** at https://lucid.app/developer:
   - **Redirect URI**: `https://vertexaisearch.cloud.google.com/oauth-redirect`
   - **Enable scopes**: `user.profile`, `offline_access`, `lucidchart.document.content`
   - Copy **Client ID** and **Client Secret**.

2. **Deploy**:
   ```bash
   ./scripts/deploy-lucid.sh --project <PROJECT_ID>
   ```

3. **Register in GE → Data stores → Create → Custom MCP Server**:
   | Field | Value |
   |---|---|
   | MCP Server URL | `<service-url>/mcp` |
   | Authorization URL | `https://lucid.app/oauth2/authorize` |
   | Authorization URL Parameters | *(leave blank; offline_access goes in Scopes)* |
   | Token URL | `https://api.lucid.co/oauth2/token` |
   | Client ID | *(your Lucid app's client_id)* |
   | Client Secret | *(your Lucid app's client secret)* |
   | Scopes | `user.profile offline_access lucidchart.document.content` |

4. **Click Login** and complete Lucid consent.

5. **Reload custom actions** on the data store.

6. **Enable tools**: `get_lucid_user_profile`, `search_lucid_documents`, `get_lucid_document_contents`.

7. **Attach to an App** (same pattern).

8. **Test**: ask GE *"Who am I in Lucid?"* or *"Search my Lucid documents for X"*.

### Troubleshooting common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| GE answers from generic docs, no `http.ingress` in logs | Org policy `discoveryengine.managed.disableCustomMcpServerConnector` still enforced | Override per Prerequisites #1 |
| `http.ingress` shows 401 from Cloud Run (not from app) | `--allow-unauthenticated` was blocked by org policy | UI-toggle "Allow unauthenticated" on the service, or override `iam.allowedPolicyMemberDomains` |
| `Reload custom actions` returns 307 | Old branch served `/mcp` with a trailing-slash redirect | Already fixed on this branch; pull latest |
| Tool returns `Access forbidden (...)` | SaaS access token lacks that scope | Append the missing scope to GE's Scopes field, re-Login |
| GE shows a textbox asking for parameters on every tool | Expected — GE confirms required string args before invoking. Zero-arg tools (e.g. `list_miro_boards`, `get_figma_me`) skip the textbox |
| Data store says "Active" but GE never dispatches | Data store attached to a Search app, not a conversational Agent | Re-attach to a Conversational/Agent app |

## Local development

```bash
pip install -r requirements.txt
ACTIVE_PROVIDER=miro python server.py
```

Health check: `curl http://localhost:8080/health`

End-to-end MCP probe (after deploy):

```bash
# Step 1: initialize
SID=$(curl -s -i -X POST "https://<service-url>/mcp" \
  -H "Authorization: Bearer dummy" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0.1"}}}' \
  | grep -i '^mcp-session-id:' | awk '{print $2}' | tr -d '\r')

# Step 2: tools/list
curl -s -X POST "https://<service-url>/mcp" \
  -H "Authorization: Bearer dummy" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

## Tests

```bash
pip install -r requirements-dev.txt
./scripts/predeploy.sh           # ruff format/check + pytest
./scripts/predeploy.sh --fix     # auto-apply lint/format
```

All downstream HTTP is mocked with `respx`. No real Miro/Figma/Lucid endpoints are touched.

## Logging

Structured JSON logs land in Cloud Logging with these events (filterable in Logs Explorer):

| Event | Meaning |
|---|---|
| `http.ingress` | GE (or anyone) hit `/mcp` |
| `http.response` | proxy returned to GE |
| `tool.invoke` / `tool.success` / `tool.error` | which MCP tool fired |
| `saas.call` | outbound call to Miro/Figma/Lucid with method, URL, status_code, latency_ms |
| `saas.401_reauth_needed` | SaaS rejected the bearer; GE needs to re-OAuth the user |

Tail:

```bash
gcloud run services logs tail mcp-miro --region us-central1 --project <PROJECT_ID>
```

## References

- [GE Custom MCP setup](https://cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server)
- [Miro REST API](https://developers.miro.com/reference/api-reference)
- [Figma REST API](https://developers.figma.com/docs/rest-api/)
- [Lucid REST API](https://developer.lucid.co/reference/api-overview)
- [MCP spec — Streamable HTTP transport](https://modelcontextprotocol.io/specification/2024-11-05/basic/transports)
