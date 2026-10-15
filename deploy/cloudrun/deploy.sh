#!/usr/bin/env bash
# Deploy Forward Defuser to Google Cloud Run.
# Prereqs (one time, see README "Deploy"): a GCP project with billing, Cloud SQL Postgres 16 instance,
# Memorystore Redis (or any Redis URL), Artifact Registry repo, and secrets created in Secret Manager.
set -euo pipefail

: "${PROJECT:?set PROJECT}"
REGION="${REGION:-asia-south1}"
REPO="${REPO:-defuser}"
SQL_INSTANCE="${SQL_INSTANCE:?set SQL_INSTANCE as project:region:instance}"
VPC_NETWORK="${VPC_NETWORK:-default}"
VPC_SUBNET="${VPC_SUBNET:-default}"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/defuser:$(git rev-parse --short HEAD)"

gcloud builds submit --project "$PROJECT" --tag "$IMAGE" .

# Secrets are referenced by name; values live only in Secret Manager.
SECRETS="DATABASE_URL=defuser-database-url:latest,REDIS_URL=defuser-redis-url:latest,DATA_KEY=defuser-data-key:latest,\
HASH_PEPPER=defuser-hash-pepper:latest,ADMIN_TOKEN=defuser-admin-token:latest,ANTHROPIC_API_KEY=defuser-anthropic-key:latest,\
TELEGRAM_BOT_TOKEN=defuser-telegram-token:latest,TELEGRAM_WEBHOOK_SECRET=defuser-telegram-webhook-secret:latest,\
GOOGLE_API_KEY=defuser-google-api-key:latest,BRAVE_API_KEY=defuser-brave-key:latest"
ENVVARS="ENV=prod,TELEGRAM_BOT_USERNAME=${TELEGRAM_BOT_USERNAME:-},ADMIN_TELEGRAM_CHAT_ID=${ADMIN_TELEGRAM_CHAT_ID:-}"

common=(--project "$PROJECT" --region "$REGION" --image "$IMAGE" --add-cloudsql-instances "$SQL_INSTANCE"
        --set-secrets "$SECRETS" --network "$VPC_NETWORK" --subnet "$VPC_SUBNET" --vpc-egress private-ranges-only)

gcloud run deploy defuser-api "${common[@]}" --allow-unauthenticated --set-env-vars "$ENVVARS,ROLE=api" \
  --min-instances 1 --max-instances 20 --concurrency 80 --cpu 1 --memory 512Mi --timeout 30

# The worker needs CPU outside of requests: no CPU throttling, always one instance.
gcloud run deploy defuser-worker "${common[@]}" --no-allow-unauthenticated --set-env-vars "$ENVVARS,ROLE=worker" \
  --min-instances 1 --max-instances 5 --no-cpu-throttling --cpu 2 --memory 2Gi

URL=$(gcloud run services describe defuser-api --project "$PROJECT" --region "$REGION" --format 'value(status.url)')
echo "API at $URL"
echo "Now register the Telegram webhook: PUBLIC_BASE_URL=$URL uv run python scripts/set_webhook.py"
