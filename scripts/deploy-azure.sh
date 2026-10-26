#!/usr/bin/env bash
# Build the image, push it to Azure Container Registry and roll out the Container App.
#   bash scripts/deploy-azure.sh             deploy the current checkout
#   bash scripts/deploy-azure.sh --config    also re-apply secrets and environment variables
# Account settings: .local/azure.env. Secrets: .local/azure-secrets.env. Both are git-ignored.
# ACR Tasks are blocked on Azure for Students, so the image is built locally with Docker.
set -euo pipefail
cd "$(dirname "$0")/.."
. .local/azure.env
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

az account set -s "$SUB"
TAG=$(git rev-parse --short HEAD)-$(date +%Y%m%d%H%M)
IMAGE=$ACR.azurecr.io/gencrag-web:$TAG

docker build --platform linux/amd64 -t "$IMAGE" .
az acr login -n "$ACR"
for attempt in 1 2 3; do docker push "$IMAGE" && break; echo "Push failed, retrying ($attempt)..."; done

if [ "${1:-}" = "--config" ]; then
  . .local/azure-secrets.env
  ID_CLIENT=$(az identity show -g "$RG" -n "$ID" --query clientId -o tsv)
  DB_URL="postgresql://$PG_ADMIN:$PG_PASSWORD@$PG.postgres.database.azure.com:5432/kbeauty?sslmode=require"
  az containerapp secret set -g "$RG" -n "$APP" -o none \
    --secrets database-url="$DB_URL" admin-password="$ADMIN_PASSWORD" secret-key="$SECRET_KEY"
  az containerapp update -g "$RG" -n "$APP" --image "$IMAGE" -o none --set-env-vars \
    DATABASE_URL=secretref:database-url ADMIN_PASSWORD=secretref:admin-password SECRET_KEY=secretref:secret-key \
    LLM_PROVIDER=foundry EMBED_DIM=1024 FOUNDRY_AUTH=entra AZURE_CLIENT_ID="$ID_CLIENT" \
    FOUNDRY_ENDPOINT="https://$AI.openai.azure.com/openai/v1/" \
    FOUNDRY_CHAT_DEPLOYMENT=gpt-4.1-mini FOUNDRY_EMBED_DEPLOYMENT=text-embedding-3-large \
    CURRENCY=USD DATE_ORDER=MDY
else
  az containerapp update -g "$RG" -n "$APP" --image "$IMAGE" -o none
fi

URL="https://$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)"
curl --fail --silent --show-error --retry 6 --retry-all-errors --retry-delay 10 --max-time 60 "$URL/health"
echo
echo "Deployed $IMAGE to $URL"
