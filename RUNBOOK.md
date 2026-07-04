# Suitcase — AWS deploy runbook

Fully automated. Two commands: `./deploy.sh up` and `./deploy.sh down`.
**Sandbox-grade** — single-AZ, minimal hardening. Meant to be applied, tested,
and destroyed the same day. Not for production traffic.

## One-time setup (before the first `up`)

1. **AWS account + CLI**
   ```
   aws configure          # access key, secret, default region
   aws sts get-caller-identity   # confirm it works
   ```
2. **Tools installed:** `terraform`, `docker` (running), `aws` CLI, `python` + your venv.
3. **Bedrock model access** (only if you use `bedrock/` model strings): AWS console →
   Bedrock → Model access → enable Claude + Titan Embeddings. If you use the
   Anthropic/OpenAI APIs directly instead, skip this.
4. **Your keys:**
   ```
   cp .env.deploy.example .env.deploy
   # edit .env.deploy — add your ANTHROPIC_API_KEY, OPENAI_API_KEY, ALERT_EMAIL
   ```

## Deploy

```
./deploy.sh up
```
What it does, in order: init → create ECR + stores (OpenSearch/RDS take ~15-20 min,
this is the slow step) → build & push the Docker image → apply Fargate + ALB + Redis
+ budget → load the corpus into cloud OpenSearch → wait for health → print the URL.

When it finishes it prints:  `Suitcase is LIVE: http://sc-....elb.amazonaws.com`

Open that URL, run some plans, take screenshots.

## Destroy (do this when done — things bill hourly)

```
./deploy.sh down
```
Destroys everything, then auto-runs `verify` to confirm nothing's left billing.
You can re-check anytime with:
```
./deploy.sh verify
```

## The three always-on billable services (what costs money)

| Service | ~cost | billed |
|---|---|---|
| OpenSearch (t3.small.search) | ~\$1/day | hourly while up |
| RDS Postgres (db.t3.micro) | ~\$0.4/day | hourly while up |
| ElastiCache Redis (t3.micro) | ~\$0.4/day | hourly while up |
| Fargate (API + worker) | ~\$1/day | hourly while up |
| ALB | ~\$0.5/day | hourly while up |
| DynamoDB / S3 | ~\$0 | pay-per-use |

A same-day up→test→down cycle is a few dollars total. The budget alarm emails you
at 50% and 90% of your `BUDGET_USD` cap.

## Troubleshooting (first-run gotchas — expect 1-2 of these)

- **OpenSearch AWS-auth / 403 on ingest:** the managed domain uses IAM signing.
  If `ingest` fails with auth errors, confirm your `aws configure` identity is
  allowed by the domain access policy, or run ingest from inside the VPC. This is
  the single most likely first-run snag.
- **Image architecture:** the build uses `--platform linux/amd64` (Fargate is x86).
  On Apple Silicon this is required — don't remove it.
- **Health check flapping:** the app needs Redis + Postgres reachable to pass
  `/health`. If the target group stays unhealthy, check the CloudWatch logs
  (`/ecs/suitcase-*`) for the API task — usually a store endpoint or a missing secret.
- **RDS in public subnet:** sandbox convenience. The SG only allows in-VPC access,
  but for anything real, move RDS/Redis to private subnets.
- **Destroy leaves an ENI/SG:** occasionally a load-balancer ENI lingers a minute.
  Re-run `./deploy.sh down`; if a security group won't delete, wait 2 min and retry.

## What to put in the portfolio README
- The architecture (this stack) + the diagram
- "one command up, one command down" as the operational story
- The store-abstraction point: local Docker → managed AWS was config-only in the app
- A note: "SQS would be the production queue; ElastiCache used here for parity with local"
- Screenshots of the live URL
