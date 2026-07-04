#!/usr/bin/env bash
# =============================================================================
# Suitcase — one-command AWS deploy / test / destroy
#
#   ./deploy.sh up       provision everything, build+push image, load data, print URL
#   ./deploy.sh down      destroy everything, then verify nothing is left billing
#   ./deploy.sh verify    check for any leftover billable resources (run after down)
#   ./deploy.sh url        just print the app URL again
#
# One-time setup before first use (see RUNBOOK.md):
#   - AWS account + `aws configure` done
#   - Bedrock model access enabled in the console (if using bedrock/ models)
#   - copy .env.deploy.example -> .env.deploy and fill in your keys
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
TF=infra/terraform
REGION="${AWS_REGION:-us-east-1}"

# --- load your keys from .env.deploy ---
if [[ -f .env.deploy ]]; then set -a; source .env.deploy; set +a; fi
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY in .env.deploy}"
: "${OPENAI_API_KEY:?set OPENAI_API_KEY in .env.deploy}"
ALERT_EMAIL="${ALERT_EMAIL:-}"
BUDGET_USD="${BUDGET_USD:-20}"

tf() { terraform -chdir="$TF" "$@"; }

tf_vars() {
  echo "-var=region=$REGION"
  echo "-var=anthropic_key=$ANTHROPIC_API_KEY"
  echo "-var=openai_key=$OPENAI_API_KEY"
  echo "-var=langfuse_public=${LANGFUSE_PUBLIC_KEY:-}"
  echo "-var=langfuse_secret=${LANGFUSE_SECRET_KEY:-}"
  echo "-var=alert_email=$ALERT_EMAIL"
  echo "-var=budget_usd=$BUDGET_USD"
}

up() {
  echo "==> [1/6] terraform init"
  tf init -input=false

  # Phase 1: create ECR first (need the repo URL to build+push before the services)
  echo "==> [2/6] creating registry + stores (this is the slow part: OpenSearch/RDS ~15-20 min)"
  IFS=$'\n' read -r -d '' -a VARS < <(tf_vars && printf '\0')
  tf apply -input=false -auto-approve "${VARS[@]}" -var="image_uri=PLACEHOLDER" \
     -target=aws_ecr_repository.app || true

  ECR_URL=$(tf output -raw ecr_repo_url)
  echo "    ECR: $ECR_URL"

  echo "==> [3/6] build + push image"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ECR_URL%/*}"
  docker build --platform linux/amd64 -f Dockerfile.deploy -t "$ECR_URL:latest" .
  docker push "$ECR_URL:latest"

  echo "==> [4/6] apply full stack (Fargate, ALB, Redis, budget)"
  tf apply -input=false -auto-approve "${VARS[@]}" -var="image_uri=$ECR_URL:latest"

  APP_URL=$(tf output -raw app_url)
  OS_HOST=$(tf output -raw opensearch_host)

  echo "==> [5/6] load data into the cloud stores"
  # run the one-off index+ingest against the managed stores, from your Mac.
  # Pin the SAME embed model/dim the app uses, so the index dimension matches
  # (OpenAI text-embedding-3-small = 1536; a mismatch here breaks retrieval).
  LOAD_ENV="DEPLOY_PROFILE=aws AWS_REGION=$REGION \
    OPENSEARCH_HOST=$OS_HOST OPENSEARCH_PORT=443 OPENSEARCH_USE_SSL=true OPENSEARCH_USE_AWS_AUTH=true \
    EMBED_MODEL=text-embedding-3-small EMBED_DIM=1536"
  env $LOAD_ENV python -m scripts.create_opensearch_index && \
  env $LOAD_ENV python -m ingest.run_ingest || echo "    (if ingest fails, see RUNBOOK troubleshooting)"

  echo "==> [6/6] waiting for the app to become healthy behind the load balancer..."
  for i in $(seq 1 30); do
    if curl -fsS "$APP_URL/health" >/dev/null 2>&1; then break; fi
    sleep 15; echo "    ...still starting ($i/30)"
  done

  echo ""
  echo "============================================================"
  echo "  Suitcase is LIVE:  $APP_URL"
  echo ""
  echo "  LOGIN (Cognito):"
  echo "    $(tf output -raw cognito_login_url 2>/dev/null)"
  echo "    user: ${TEST_USER_EMAIL:-rahulfreescale@gmail.com}"
  echo "    temp password: check that inbox (Cognito emailed it on create)"
  echo "    first login forces you to set a real password"
  echo ""
  echo "  Budget alarm set at \$$BUDGET_USD/mo"
  echo "  When done:  ./deploy.sh down"
  echo "============================================================"
}

down() {
  echo "==> destroying EVERYTHING"
  IFS=$'\n' read -r -d '' -a VARS < <(tf_vars && printf '\0')
  # image_uri may be gone; pass a placeholder so vars validate
  tf destroy -input=false -auto-approve "${VARS[@]}" -var="image_uri=PLACEHOLDER"
  echo "==> destroy complete. running verify..."
  verify
}

verify() {
  echo "==> checking for leftover billable resources tagged Project=suitcase ..."
  echo "-- Fargate services (should be empty):"
  aws ecs list-services --cluster suitcase-cluster --region "$REGION" 2>/dev/null || echo "   cluster gone ✔"
  echo "-- OpenSearch domains (should not list suitcase-kb):"
  aws opensearch list-domain-names --region "$REGION" --query "DomainNames[?DomainName=='suitcase-kb']" 2>/dev/null
  echo "-- RDS instances (should not list suitcase-checkpointer):"
  aws rds describe-db-instances --region "$REGION" --query "DBInstances[?DBInstanceIdentifier=='suitcase-checkpointer'].DBInstanceIdentifier" 2>/dev/null
  echo "-- ElastiCache (should not list suitcase-redis):"
  aws elasticache describe-cache-clusters --region "$REGION" --query "CacheClusters[?CacheClusterId=='suitcase-redis'].CacheClusterId" 2>/dev/null
  echo "-- Load balancers (should not list an sc-* LB):"
  aws elbv2 describe-load-balancers --region "$REGION" --query "LoadBalancers[?starts_with(LoadBalancerName,'sc-')].LoadBalancerName" 2>/dev/null
  echo ""
  echo "If every line above is empty/✔, nothing is billing. If anything lists a"
  echo "resource, re-run ./deploy.sh down, or delete it by hand in the console."
}

case "${1:-}" in
  up)     up ;;
  down)   down ;;
  verify) verify ;;
  url)    tf output -raw app_url ;;
  *) echo "usage: ./deploy.sh {up|down|verify|url}"; exit 1 ;;
esac
