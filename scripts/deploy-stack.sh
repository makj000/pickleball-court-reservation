#!/bin/bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
set -a; source "$ROOT_DIR/.env" 2>/dev/null || true; set +a

REGION="${REGION:-us-west-2}"
STACK_NAME="${STACK_NAME:-pickleball-monitor-infra}"
TEMPLATE="$ROOT_DIR/infra/pickleball-monitor.yaml"
PROJECT_TAG="${PROJECT_TAG:-Pickleball}"
SERVICE_TAG="${SERVICE_TAG:-pickleball-monitor}"

: "${FUNCTION:?set FUNCTION}"
: "${UI_BUCKET:?set UI_BUCKET}"
: "${QUEUE_NAME:?set QUEUE_NAME}"

ROLE_ARN=$(aws lambda get-function-configuration \
  --function-name "$FUNCTION" \
  --region "$REGION" \
  --query 'Role' \
  --output text)
ROLE_NAME="${ROLE_ARN##*/}"
QUEUE_POLICY_NAME="${QUEUE_POLICY_NAME:-${QUEUE_NAME}-queue}"

echo "==> Deploying CloudFormation stack $STACK_NAME..."
aws cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION" \
  --tags \
    Project="$PROJECT_TAG" \
    Service="$SERVICE_TAG" \
    Stack="$STACK_NAME" \
  --parameter-overrides \
    FunctionName="$FUNCTION" \
    LambdaRoleName="$ROLE_NAME" \
    UiBucketName="$UI_BUCKET" \
    WorkQueueName="$QUEUE_NAME" \
    QueuePolicyName="$QUEUE_POLICY_NAME"

QUEUE_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`WorkQueueUrl`].OutputValue | [0]' \
  --output text)

echo "==> Ensuring Lambda uses stack queue..."
ENV_IN=$(mktemp)
ENV_OUT=$(mktemp)
aws lambda get-function-configuration \
  --function-name "$FUNCTION" \
  --region "$REGION" \
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
  --function-name "$FUNCTION" \
  --environment "file://$ENV_OUT" \
  --region "$REGION" \
  --output text \
  --query 'LastUpdateStatus'
aws lambda wait function-updated \
  --function-name "$FUNCTION" \
  --region "$REGION"
rm -f "$ENV_IN" "$ENV_OUT"

echo "==> Stack deployment complete."
