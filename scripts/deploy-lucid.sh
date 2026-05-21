#!/usr/bin/env bash
#
# Deploy the Lucid flavor of the Gemini Enterprise Custom MCP Proxy to Cloud Run.
#
# Required values (pass as flag OR export as env var):
#   --project               | PROJECT               GCP project id
#   --google-client-id      | GOOGLE_CLIENT_ID      OAuth client id GE uses to call this proxy
#   --lucid-client-id       | LUCID_CLIENT_ID       Lucid app client id
#   --lucid-client-secret   | LUCID_CLIENT_SECRET   Lucid app client secret
#
# Optional:
#   --env-file <path>                               Source a shell-format env file BEFORE
#                                                   reading defaults (recommended for customer envs)
#   --region                | REGION                Cloud Run region (default: us-central1)
#   --service-name          | SERVICE_NAME          Cloud Run service name (default: mcp-lucid)
#   --allowed-origins       | ALLOWED_ORIGINS       CORS allow-list (default: https://vertexaisearch.cloud.google.com)
#   --no-allow-unauthenticated                      Require IAM auth at the network layer.
#                                                   Default is --allow-unauthenticated; the proxy
#                                                   still validates Google bearer tokens itself.
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
GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-}"
LUCID_CLIENT_ID="${LUCID_CLIENT_ID:-}"
LUCID_CLIENT_SECRET="${LUCID_CLIENT_SECRET:-}"
ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-https://vertexaisearch.cloud.google.com}"
ALLOW_UNAUTH="--allow-unauthenticated"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)                 shift 2 ;;  # already consumed in the first pass
    --project)                  PROJECT="$2"; shift 2 ;;
    --region)                   REGION="$2"; shift 2 ;;
    --service-name)             SERVICE_NAME="$2"; shift 2 ;;
    --google-client-id)         GOOGLE_CLIENT_ID="$2"; shift 2 ;;
    --lucid-client-id)          LUCID_CLIENT_ID="$2"; shift 2 ;;
    --lucid-client-secret)      LUCID_CLIENT_SECRET="$2"; shift 2 ;;
    --allowed-origins)          ALLOWED_ORIGINS="$2"; shift 2 ;;
    --allow-unauthenticated)    ALLOW_UNAUTH="--allow-unauthenticated"; shift ;;
    --no-allow-unauthenticated) ALLOW_UNAUTH="--no-allow-unauthenticated"; shift ;;
    -h|--help)                  usage ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

mask() {
  local v="$1"
  [[ -z "$v" ]] && { echo "<missing>"; return; }
  local n=${#v}
  if (( n <= 4 )); then echo "****"; else echo "${v:0:2}***${v: -2} (len=$n)"; fi
}

cat <<EOF
================================================================================
Deployment configuration (Lucid)
--------------------------------------------------------------------------------
  PROJECT             : ${PROJECT:-<missing>}
  REGION              : ${REGION}
  SERVICE_NAME        : ${SERVICE_NAME}
  GOOGLE_CLIENT_ID    : ${GOOGLE_CLIENT_ID:-<missing>}
  LUCID_CLIENT_ID     : $(mask "${LUCID_CLIENT_ID}")
  LUCID_CLIENT_SECRET : $(mask "${LUCID_CLIENT_SECRET}")
  ALLOWED_ORIGINS     : ${ALLOWED_ORIGINS}
  AUTH MODE           : ${ALLOW_UNAUTH}
================================================================================
EOF

missing=()
for var in PROJECT GOOGLE_CLIENT_ID LUCID_CLIENT_ID LUCID_CLIENT_SECRET; do
  if [[ -z "${!var}" ]]; then
    flag_name="${var,,}"
    flag_name="${flag_name//_/-}"
    missing+=("--${flag_name} (or env: ${var})")
  fi
done
if (( ${#missing[@]} > 0 )); then
  printf 'ERROR: Missing required value(s):\n' >&2
  printf '  - %s\n' "${missing[@]}" >&2
  exit 1
fi

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is required on PATH for local builds." >&2; exit 1; }

echo ">> Enabling required GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com \
  --project="$PROJECT"

echo ">> Upserting Secret Manager entries..."
upsert_secret() {
  local name="$1"
  local value="$2"
  if gcloud secrets describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --project="$PROJECT"
  else
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic --project="$PROJECT"
  fi
}
upsert_secret "lucid-client-id"     "$LUCID_CLIENT_ID"
upsert_secret "lucid-client-secret" "$LUCID_CLIENT_SECRET"

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
  --set-env-vars="ACTIVE_PROVIDER=lucid,GCP_PROJECT_ID=${PROJECT},GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID},ALLOWED_ORIGINS=${ALLOWED_ORIGINS}" \
  --set-secrets="LUCID_CLIENT_ID=lucid-client-id:latest,LUCID_CLIENT_SECRET=lucid-client-secret:latest"

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT" --format='value(status.url)')
REDIRECT_URI="${SERVICE_URL}/auth/lucid/callback"

echo ">> Updating LUCID_REDIRECT_URI to ${REDIRECT_URI}..."
gcloud run services update "$SERVICE_NAME" \
  --region "$REGION" \
  --project "$PROJECT" \
  --update-env-vars="LUCID_REDIRECT_URI=${REDIRECT_URI}"

cat <<EOF

================================================================================
Lucid MCP proxy deployed.

  Service URL:   ${SERVICE_URL}
  MCP endpoint:  ${SERVICE_URL}/mcp
  Health check:  ${SERVICE_URL}/health

Next steps:
  1. In the Lucid developer console (https://developer.lucid.co/), set the OAuth
     redirect URI to:
       ${REDIRECT_URI}
  2. In Gemini Enterprise, register a new Custom MCP Server data store with:
       MCP server URL: ${SERVICE_URL}/mcp
================================================================================
EOF
