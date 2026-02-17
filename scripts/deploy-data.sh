#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "Error on line $LINENO"; exit 1' ERR

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$ROOT_DIR/data"

if [ ! -d "$DATA_DIR" ]; then
  echo "Data directory not found: $DATA_DIR"
  exit 1
fi

AWS_PAGER=""
export AWS_PAGER

aws s3 sync "$DATA_DIR" s3://studio-data.humblyproud.com/ --delete --exclude ".DS_Store"

DISTRIBUTION_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Aliases.Items[?@=='humblyproud.com']].Id" \
  --output text --no-cli-pager)

if [ -z "$DISTRIBUTION_ID" ] || [ "$DISTRIBUTION_ID" = "None" ]; then
  echo "Could not resolve CloudFront distribution for humblyproud.com"
  exit 1
fi

aws cloudfront create-invalidation \
  --distribution-id "$DISTRIBUTION_ID" \
  --paths "/studio-data/*"
