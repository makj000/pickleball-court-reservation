#!/bin/bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
set -a; source "$ROOT_DIR/.env" 2>/dev/null || true; set +a

REGION="${REGION:-us-west-2}"
STACK_NAME="${STACK_NAME:-pickleball-monitor-infra}"
PROJECT_TAG="${PROJECT_TAG:-Pickleball}"
SERVICE_TAG="${SERVICE_TAG:-pickleball-monitor}"

: "${FUNCTION:?set FUNCTION}"
: "${UI_BUCKET:?set UI_BUCKET}"
: "${QUEUE_NAME:?set QUEUE_NAME}"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
QUEUE_URL=$(aws sqs get-queue-url \
  --queue-name "$QUEUE_NAME" \
  --region "$REGION" \
  --query 'QueueUrl' \
  --output text)

COMMON_TAGS=(
  Key=Project,Value="$PROJECT_TAG"
  Key=Service,Value="$SERVICE_TAG"
  Key=Stack,Value="$STACK_NAME"
)

echo "==> Tagging CloudFormation stack..."
aws cloudformation update-stack \
  --stack-name "$STACK_NAME" \
  --use-previous-template \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION" \
  --parameters \
    ParameterKey=FunctionName,UsePreviousValue=true \
    ParameterKey=LambdaRoleName,UsePreviousValue=true \
    ParameterKey=UiBucketName,UsePreviousValue=true \
    ParameterKey=WorkQueueName,UsePreviousValue=true \
    ParameterKey=QueuePolicyName,UsePreviousValue=true \
  --tags \
    Key=Project,Value="$PROJECT_TAG" \
    Key=Service,Value="$SERVICE_TAG" \
    Key=Stack,Value="$STACK_NAME" >/dev/null || true
aws cloudformation wait stack-update-complete \
  --stack-name "$STACK_NAME" \
  --region "$REGION" 2>/dev/null || true

echo "==> Tagging S3 bucket..."
aws s3api put-bucket-tagging \
  --bucket "$UI_BUCKET" \
  --tagging "TagSet=[{Key=Project,Value=$PROJECT_TAG},{Key=Service,Value=$SERVICE_TAG},{Key=Stack,Value=$STACK_NAME}]" >/dev/null

echo "==> Tagging SQS queue..."
aws sqs tag-queue \
  --queue-url "$QUEUE_URL" \
  --tags Project="$PROJECT_TAG",Service="$SERVICE_TAG",Stack="$STACK_NAME"

echo "==> Tagging Lambda functions..."
for fn in "$FUNCTION" pickleball-authorizer; do
  arn=$(aws lambda get-function-configuration \
    --function-name "$fn" \
    --region "$REGION" \
    --query 'FunctionArn' \
    --output text 2>/dev/null || true)
  if [ -n "$arn" ] && [ "$arn" != "None" ]; then
    aws lambda tag-resource \
      --resource "$arn" \
      --tags Project="$PROJECT_TAG",Service="$SERVICE_TAG",Stack="$STACK_NAME"
  fi
done

echo "==> Tagging ECR repository..."
aws ecr tag-resource \
  --resource-arn "arn:aws:ecr:$REGION:$ACCOUNT_ID:repository/pickleball-monitor" \
  --tags "${COMMON_TAGS[@]}"

echo "==> Tagging EventBridge rules..."
for rule in \
  booking-agent-prep \
  booking-agent-report \
  pickleball-hourly-scan \
  pickleball-probe-before-8am \
  pickleball-probe-after-8am \
  pickleball-probe-before-9am \
  pickleball-probe-after-9am
do
  arn=$(aws events describe-rule \
    --name "$rule" \
    --region "$REGION" \
    --query 'Arn' \
    --output text 2>/dev/null || true)
  if [ -n "$arn" ] && [ "$arn" != "None" ]; then
    aws events tag-resource \
      --resource-arn "$arn" \
      --tags "${COMMON_TAGS[@]}"
  fi
done

echo "==> Tagging API Gateway..."
api_id=$(aws apigatewayv2 get-apis \
  --region "$REGION" \
  --query 'Items[?Name==`pickleball-monitor-api`].ApiId | [0]' \
  --output text 2>/dev/null || true)
if [ -n "$api_id" ] && [ "$api_id" != "None" ]; then
  aws apigatewayv2 tag-resource \
    --resource-arn "arn:aws:apigateway:$REGION::/apis/$api_id" \
    --tags Project="$PROJECT_TAG",Service="$SERVICE_TAG",Stack="$STACK_NAME"
fi

echo "==> Tagging CloudWatch log groups..."
for log_group in /aws/lambda/pickleball-monitor /aws/lambda/pickleball-authorizer; do
  aws logs tag-resource \
    --resource-arn "arn:aws:logs:$REGION:$ACCOUNT_ID:log-group:$log_group" \
    --tags Project="$PROJECT_TAG",Service="$SERVICE_TAG",Stack="$STACK_NAME" 2>/dev/null || \
  aws logs tag-log-group \
    --log-group-name "$log_group" \
    --tags Project="$PROJECT_TAG",Service="$SERVICE_TAG",Stack="$STACK_NAME" 2>/dev/null || true
done

echo "==> Tagging IAM role..."
aws iam tag-role \
  --role-name pickleball-lambda-role \
  --tags "${COMMON_TAGS[@]}"

echo "==> Tagging complete."
