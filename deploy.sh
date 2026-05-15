#!/bin/bash
set -e

ECR_URI="123456789012.dkr.ecr.us-west-2.amazonaws.com/your-ecr-repo:latest"
REGION="us-west-2"
FUNCTION="your-lambda-function-name"
UI_BUCKET="your-ui-s3-bucket"
FUNCTION_URL_INVOKE_SID="public-function-url-invoke"

echo "==> Uploading UI to S3..."
aws s3 cp ui/index.html s3://$UI_BUCKET/index.html \
  --content-type "text/html" \
  --cache-control "no-store, max-age=0" \
  --region $REGION

echo "==> Ensuring Function URL invoke permission..."
if ! aws lambda get-policy \
  --function-name $FUNCTION \
  --region $REGION \
  --query 'Policy' \
  --output text 2>/dev/null | grep -q "\"Sid\":\"$FUNCTION_URL_INVOKE_SID\""
then
  aws lambda add-permission \
    --function-name $FUNCTION \
    --statement-id $FUNCTION_URL_INVOKE_SID \
    --action lambda:InvokeFunction \
    --principal '*' \
    --invoked-via-function-url \
    --region $REGION >/dev/null
fi

echo "==> Authenticating Docker to ECR..."
aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin \
  123456789012.dkr.ecr.us-west-2.amazonaws.com

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

echo "==> Triggering forced state refresh..."
aws lambda invoke \
  --function-name $FUNCTION \
  --invocation-type Event \
  --cli-binary-format raw-in-base64-out \
  --payload '{"_scheduled":true}' \
  /tmp/pickleball-deploy-refresh.json \
  --region $REGION >/dev/null

echo "==> Registering Telegram webhook..."
# Load .env for TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET
set -a; source "$(dirname "$0")/.env" 2>/dev/null || true; set +a
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

echo "==> Done. Lambda is live, state refresh started, Telegram webhook registered."
