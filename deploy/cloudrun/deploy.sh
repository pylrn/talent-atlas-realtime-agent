#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${1:-}"
REGION="${2:-us-central1}"
SERVICE="${3:-talent-atlas}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "Usage: deploy/cloudrun/deploy.sh <gcp-project-id> [region] [service-name]" >&2
  exit 2
fi
if [[ ! -f .env ]]; then
  echo "A local .env file is required to seed Cloud Secret Manager." >&2
  exit 2
fi

env_value() {
  local key="$1"
  awk -v key="$key" 'index($0, key "=") == 1 {sub("^[^=]*=", ""); value=$0} END {printf "%s", value}' .env
}

put_secret() {
  local env_key="$1"
  local secret_name="$2"
  local value
  value="$(env_value "$env_key")"
  if [[ -z "$value" || "$value" == *"..."* ]]; then
    echo "Missing usable $env_key in .env" >&2
    exit 2
  fi
  if ! gcloud secrets describe "$secret_name" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud secrets create "$secret_name" --project "$PROJECT_ID" --replication-policy automatic >/dev/null
  fi
  printf '%s' "$value" | gcloud secrets versions add "$secret_name" \
    --project "$PROJECT_ID" --data-file=- >/dev/null
  unset value
}

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  --project "$PROJECT_ID"

REPOSITORY="talent-atlas"
if ! gcloud artifacts repositories describe "$REPOSITORY" \
  --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPOSITORY" \
    --repository-format docker --location "$REGION" --project "$PROJECT_ID"
fi

put_secret DATABASE_URL talent-atlas-database-url
put_secret DEEPSEEK_API_KEY talent-atlas-deepseek-key
put_secret GROQ_API_KEY talent-atlas-groq-key

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNTIME_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for secret in talent-atlas-database-url talent-atlas-deepseek-key talent-atlas-groq-key; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --project "$PROJECT_ID" \
    --member "serviceAccount:${RUNTIME_ACCOUNT}" \
    --role roles/secretmanager.secretAccessor >/dev/null
done

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/app:$(git rev-parse --short HEAD)"
gcloud builds submit . \
  --project "$PROJECT_ID" \
  --config deploy/cloudrun/cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE}"

gcloud run deploy "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --image "$IMAGE" \
  --allow-unauthenticated \
  --cpu 1 \
  --memory 1Gi \
  --min 0 \
  --max 1 \
  --concurrency 2 \
  --timeout 300 \
  --env-vars-file deploy/cloudrun/env.yaml \
  --set-secrets 'DATABASE_URL=talent-atlas-database-url:latest,DEEPSEEK_API_KEY=talent-atlas-deepseek-key:latest,GROQ_API_KEY=talent-atlas-groq-key:latest'

gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)'
