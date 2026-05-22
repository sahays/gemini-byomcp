#!/usr/bin/env bash
#
# Deploy the Lucid flavor (passthrough variant) of the Gemini Enterprise Custom
# MCP Proxy to Cloud Run.
#
# This branch (option-a-passthrough) does NOT need the Lucid client_id/secret —
# Gemini Enterprise holds those and runs the OAuth flow itself. The proxy just
# forwards the bearer token GE sends it.
#
# Required values (pass as flag OR export as env var):
#   --project   | PROJECT   GCP project id
#
# Optional:
#   --env-file <path>                             Source a shell-format env file BEFORE
#                                                 reading defaults (recommended for customer envs)
#   --region          | REGION         Cloud Run region (default: us-central1)
#   --service-name    | SERVICE_NAME   Cloud Run service name (default: mcp-lucid)
#   --allowed-origins | ALLOWED_ORIGINS  CORS allow-list (default: https://vertexaisearch.cloud.google.com)
#   --no-allow-unauthenticated                    Require IAM auth at the network layer.
#                                                 Default is --allow-unauthenticated; the proxy
#                                                 verifies the SaaS bearer token by using it.
#
set -euo pipefail

usage() {
  awk '/^#!/{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

# First pass: load --env-file BEFORE we snapshot defaults from the environment.
args=("$@")
i=0
while (( i < ${#args[@]} )); do
  if [[ "${args[i]}" == "--env-file" ]]; then
    env_file="${args[i+1]:-}"
    if [[ -z "$env_file" || ! -f "$env_file" ]]; then
      echo "ERROR: --env-file requires a path to an existing file (got '${env_file}')" >&2
      exit 1
    fi
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
    break
  fi
  i=$((i + 1))
done

# Defaults pulled from environment if present.
PROJECT="${PROJECT:-}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-mcp-lucid}"
ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-https://vertexaisearch.cloud.google.com}"
ALLOW_UNAUTH="--allow-unauthenticated"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)                 shift 2 ;;
    --project)                  PROJECT="$2"; shift 2 ;;
    --region)                   REGION="$2"; shift 2 ;;
    --service-name)             SERVICE_NAME="$2"; shift 2 ;;
    --allowed-origins)          ALLOWED_ORIGINS="$2"; shift 2 ;;
    --allow-unauthenticated)    ALLOW_UNAUTH="--allow-unauthenticated"; shift ;;
    --no-allow-unauthenticated) ALLOW_UNAUTH="--no-allow-unauthenticated"; shift ;;
    -h|--help)                  usage ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

cat <<EOF
================================================================================
Deployment configuration (Lucid — passthrough)
--------------------------------------------------------------------------------
  PROJECT             : ${PROJECT:-<missing>}
  REGION              : ${REGION}
  SERVICE_NAME        : ${SERVICE_NAME}
  ALLOWED_ORIGINS     : ${ALLOWED_ORIGINS}
  AUTH MODE           : ${ALLOW_UNAUTH}
================================================================================
EOF

if [[ -z "$PROJECT" ]]; then
  echo "ERROR: --project (or env: PROJECT) is required." >&2
  exit 1
fi

command -v docker >/dev/null 2>&1 || {
  echo "ERROR: docker is required on PATH for local builds." >&2
  exit 1
}

echo ">> Enabling required GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  --project="$PROJECT"

AR_REPO="mcp-proxy"
AR_HOST="${REGION}-docker.pkg.dev"
IMAGE="${AR_HOST}/${PROJECT}/${AR_REPO}/${SERVICE_NAME}:$(date +%Y%m%d-%H%M%S)"

echo ">> Ensuring Artifact Registry repo ${AR_REPO} exists in ${REGION}..."
if ! gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Gemini Enterprise Custom MCP Proxy images" \
    --project="$PROJECT"
fi

echo ">> Configuring Docker auth for ${AR_HOST}..."
gcloud auth configure-docker "$AR_HOST" --quiet

echo ">> Building image locally: ${IMAGE}"
docker build -t "$IMAGE" "$REPO_ROOT"

echo ">> Pushing image to Artifact Registry..."
docker push "$IMAGE"

echo ">> Deploying ${SERVICE_NAME} to Cloud Run (region: ${REGION})..."
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --region "$REGION" \
  --project "$PROJECT" \
  $ALLOW_UNAUTH \
  --set-env-vars="ACTIVE_PROVIDER=lucid,ALLOWED_ORIGINS=${ALLOWED_ORIGINS}"

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT" --format='value(status.url)')

cat <<EOF

================================================================================
Lucid MCP proxy (passthrough) deployed.

  Service URL:   ${SERVICE_URL}
  MCP endpoint:  ${SERVICE_URL}/mcp
  Health check:  ${SERVICE_URL}/health

Next steps for Gemini Enterprise:
  1. In the Lucid developer console (https://lucid.app/developer), set the OAuth
     redirect URI to:
       https://vertexaisearch.cloud.google.com/oauth-redirect

  2. In Gemini Enterprise → Data stores → Create → Custom MCP Server,
     fill the "Authentication settings" dialog with:
       MCP Server URL       : ${SERVICE_URL}/mcp
       Authorization URL    : https://lucid.app/oauth2/authorize
       Auth URL Parameters  : (leave blank; offline_access goes in Scopes)
       Token URL            : https://api.lucid.co/oauth2/token
       Client ID            : <your Lucid app's client_id>
       Client Secret        : <your Lucid app's client secret>
       Scopes               : user.profile offline_access lucidchart.document.content
  3. Click Login in the dialog and complete the consent flow.
================================================================================
EOF
