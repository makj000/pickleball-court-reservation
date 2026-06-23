#!/bin/bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
set -a; source "$SCRIPT_DIR/.env" 2>/dev/null || true; set +a

trap 'echo ""; echo "==> FAILED at line $LINENO" >&2' ERR

REGION="${REGION:-us-west-2}"
FUNCTION_URL_INVOKE_SID="${FUNCTION_URL_INVOKE_SID:-public-function-url-invoke}"

: "${ECR_URI:?set ECR_URI, e.g. 123456789012.dkr.ecr.us-west-2.amazonaws.com/your-ecr-repo:latest}"
: "${FUNCTION:?set FUNCTION}"
: "${UI_BUCKET:?set UI_BUCKET}"
: "${QUEUE_NAME:?set QUEUE_NAME}"

QUEUE_POLICY_NAME="${QUEUE_POLICY_NAME:-${QUEUE_NAME}-queue}"

ECR_REGISTRY="${ECR_URI%%/*}"

UI_SOURCE="$SCRIPT_DIR/ui/index.html"
UI_VERSION=$(grep -o "UI_VERSION = '[^']*'" "$UI_SOURCE" | grep -o "'[^']*'" | tr -d "'")

echo "==> Ensuring S3 bucket policy..."
aws s3api put-bucket-policy --bucket "$UI_BUCKET" --region $REGION --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Effect\": \"Allow\",
    \"Principal\": \"*\",
    \"Action\": \"s3:GetObject\",
    \"Resource\": [
      \"arn:aws:s3:::${UI_BUCKET}/index.html\",
      \"arn:aws:s3:::${UI_BUCKET}/config.js\",
      \"arn:aws:s3:::${UI_BUCKET}/img/*\"
    ]
  }]
}"

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

echo "==> Ensuring SQS work queue..."
QUEUE_URL=$(aws sqs get-queue-url \
  --queue-name $QUEUE_NAME \
  --region $REGION \
  --query 'QueueUrl' \
  --output text 2>/dev/null || true)
if [ -z "$QUEUE_URL" ] || [ "$QUEUE_URL" = "None" ]; then
  QUEUE_URL=$(aws sqs create-queue \
    --queue-name $QUEUE_NAME \
    --attributes VisibilityTimeout=1200,ReceiveMessageWaitTimeSeconds=20,MessageRetentionPeriod=86400 \
    --region $REGION \
    --query 'QueueUrl' \
    --output text)
fi
QUEUE_ARN=$(aws sqs get-queue-attributes \
  --queue-url "$QUEUE_URL" \
  --attribute-names QueueArn \
  --region $REGION \
  --query 'Attributes.QueueArn' \
  --output text)

echo "==> Ensuring Lambda can use SQS..."
ROLE_ARN=$(aws lambda get-function-configuration \
  --function-name $FUNCTION \
  --region $REGION \
  --query 'Role' \
  --output text)
ROLE_NAME="${ROLE_ARN##*/}"
POLICY_FILE=$(mktemp)
cat > "$POLICY_FILE" <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ChangeMessageVisibility"
      ],
      "Resource": "$QUEUE_ARN"
    }
  ]
}
POLICY
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "$QUEUE_POLICY_NAME" \
  --policy-document "file://$POLICY_FILE"
rm -f "$POLICY_FILE"

echo "==> Setting Lambda queue environment..."
ENV_IN=$(mktemp)
ENV_OUT=$(mktemp)
aws lambda get-function-configuration \
  --function-name $FUNCTION \
  --region $REGION \
  --query 'Environment.Variables' \
  --output json > "$ENV_IN"
python3 - "$ENV_IN" "$ENV_OUT" "$QUEUE_URL" <<'PY'
import json
import sys

env_in, env_out, queue_url = sys.argv[1:]
with open(env_in, "r", encoding="utf-8") as f:
    variables = json.load(f) or {}
variables["PICKLEBALL_QUEUE_URL"] = queue_url
with open(env_out, "w", encoding="utf-8") as f:
    json.dump({"Variables": variables}, f)
PY
aws lambda update-function-configuration \
  --function-name $FUNCTION \
  --environment "file://$ENV_OUT" \
  --region $REGION \
  --output text \
  --query 'LastUpdateStatus'
aws lambda wait function-updated \
  --function-name $FUNCTION \
  --region $REGION
rm -f "$ENV_IN" "$ENV_OUT"

echo "==> Ensuring SQS event source mapping..."
MAPPING_UUID=$(aws lambda list-event-source-mappings \
  --function-name $FUNCTION \
  --event-source-arn "$QUEUE_ARN" \
  --region $REGION \
  --query 'EventSourceMappings[0].UUID' \
  --output text)
if [ -z "$MAPPING_UUID" ] || [ "$MAPPING_UUID" = "None" ]; then
  aws lambda create-event-source-mapping \
    --function-name $FUNCTION \
    --event-source-arn "$QUEUE_ARN" \
    --batch-size 1 \
    --enabled \
    --region $REGION >/dev/null
else
  aws lambda update-event-source-mapping \
    --uuid "$MAPPING_UUID" \
    --batch-size 1 \
    --enabled \
    --region $REGION >/dev/null
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

echo "==> Ensuring booking agent EventBridge rules..."
LAMBDA_ARN=$(aws lambda get-function \
  --function-name $FUNCTION \
  --region $REGION \
  --query 'Configuration.FunctionArn' \
  --output text)

for RULE_NAME in booking-agent-prep booking-agent-report; do
  if [ "$RULE_NAME" = "booking-agent-prep" ]; then
    # 7:30 AM PDT (UTC-7) = 14:30 UTC, weekends only
    CRON="cron(30 14 ? * SAT,SUN *)"
    PAYLOAD='{"_booking_agent":true,"phase":"prep"}'
  else
    # 8:30 AM PDT (UTC-7) = 15:30 UTC
    CRON="cron(30 15 * * ? *)"
    PAYLOAD='{"_booking_agent":true,"phase":"report"}'
  fi
  aws events put-rule \
    --name "$RULE_NAME" \
    --schedule-expression "$CRON" \
    --state ENABLED \
    --region $REGION >/dev/null
  aws events put-targets \
    --rule "$RULE_NAME" \
    --targets "Id=lambda,Arn=$LAMBDA_ARN,Input='$PAYLOAD'" \
    --region $REGION >/dev/null
  aws lambda add-permission \
    --function-name $FUNCTION \
    --statement-id "${RULE_NAME}-invoke" \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn "arn:aws:events:$REGION:$(aws sts get-caller-identity --query Account --output text):rule/$RULE_NAME" \
    --region $REGION >/dev/null 2>&1 || true
  echo "    $RULE_NAME: $CRON"
done

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
