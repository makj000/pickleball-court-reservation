#!/bin/bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
set -a; source "$SCRIPT_DIR/.env" 2>/dev/null || true; set +a

trap 'echo ""; echo "==> FAILED at line $LINENO" >&2' ERR

REGION="${REGION:-us-west-2}"

: "${ECR_URI:?set ECR_URI, e.g. 123456789012.dkr.ecr.us-west-2.amazonaws.com/your-ecr-repo:latest}"
: "${FUNCTION:?set FUNCTION}"
: "${UI_BUCKET:?set UI_BUCKET}"
: "${QUEUE_NAME:?set QUEUE_NAME}"

ECR_REGISTRY="${ECR_URI%%/*}"

UI_SOURCE="$SCRIPT_DIR/ui/index.html"
UI_VERSION=$(grep -o "UI_VERSION = '[^']*'" "$UI_SOURCE" | grep -o "'[^']*'" | tr -d "'")

echo "==> Ensuring infrastructure stack..."
"$SCRIPT_DIR/scripts/deploy-stack.sh"

echo "==> Uploading config.js to S3..."
CONFIG_JS=$(mktemp)
echo "window.PICKLEBALL_API_BASE = '${API_BASE:-}';" > "$CONFIG_JS"
aws s3 cp "$CONFIG_JS" "s3://$UI_BUCKET/config.js" \
  --content-type "application/javascript" \
  --cache-control "no-store, max-age=0" \
  --region $REGION
rm -f "$CONFIG_JS"
echo "    config.js uploaded (API_BASE=${API_BASE:-<empty>})."

echo "==> Uploading UI v${UI_VERSION:-?} to S3..."
aws s3 cp "$UI_SOURCE" "s3://$UI_BUCKET/index.html" \
  --content-type "text/html" \
  --cache-control "no-store, max-age=0" \
  --region $REGION
echo "    UI v${UI_VERSION:-?} uploaded."

if [ -d "$SCRIPT_DIR/img" ]; then
  aws s3 sync "$SCRIPT_DIR/img" "s3://$UI_BUCKET/img" \
    --cache-control "max-age=31536000" \
    --region $REGION
fi

echo "==> Authenticating Docker to ECR..."
aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin \
  "$ECR_REGISTRY"

echo "==> Building and pushing image..."
docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --output type=registry \
  -t $ECR_URI \
  .

echo "==> Updating Lambda..."
aws lambda update-function-code \
  --function-name $FUNCTION \
  --image-uri $ECR_URI \
  --region $REGION \
  --output text \
  --query 'LastUpdateStatus'

echo "==> Waiting for Lambda to be ready..."
aws lambda wait function-updated \
  --function-name $FUNCTION \
  --region $REGION

echo "==> Registering Telegram webhook..."
LAMBDA_URL=$(aws lambda get-function-url-config \
  --function-name $FUNCTION \
  --region $REGION \
  --query 'FunctionUrl' \
  --output text 2>/dev/null || echo "")
if [ -z "$LAMBDA_URL" ] || [ -z "$TELEGRAM_BOT_TOKEN" ]; then
  echo "  Skipped: LAMBDA_URL or TELEGRAM_BOT_TOKEN not available."
else
  WEBHOOK_URL="${LAMBDA_URL}telegram"
  SECRET="${TELEGRAM_WEBHOOK_SECRET:-${API_PASSWORD:-}}"
  RESULT=$(curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
    --data-urlencode "url=${WEBHOOK_URL}" \
    ${SECRET:+--data-urlencode "secret_token=${SECRET}"})
  echo "  $RESULT"
fi

echo "==> Done. Lambda is live, Telegram webhook registered."
