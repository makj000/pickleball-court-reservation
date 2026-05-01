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

echo "==> Done. Lambda is live and state refresh started."
