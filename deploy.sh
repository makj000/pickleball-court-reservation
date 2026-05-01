#!/bin/bash
set -e

ECR_URI="123456789012.dkr.ecr.us-west-2.amazonaws.com/your-ecr-repo:latest"
REGION="us-west-2"
FUNCTION="your-lambda-function-name"
UI_BUCKET="your-ui-s3-bucket"

echo "==> Uploading UI to S3..."
aws s3 cp ui/index.html s3://$UI_BUCKET/index.html \
  --content-type "text/html" \
  --region $REGION

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

echo "==> Done. Lambda is live."
