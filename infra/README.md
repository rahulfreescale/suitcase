# Infra reference

Local dev uses `docker-compose.yml` at the repo root. These files are for the
**AWS-faithful** deployment.

| File | Purpose |
|------|---------|
| `opensearch_index.json` | Index mapping (knn_vector + text + metadata) for Amazon OpenSearch Service |
| `glue_table.sql` | Athena/Glue external table over the S3 studies data lake |
| `dynamodb_table.json` | App-state table definition |
| `terraform/main.tf` | Starter IaC for the four managed services |

Production service mapping:

| Demo (local)        | Production (AWS)                |
|---------------------|--------------------------------|
| OpenSearch (docker) | Amazon OpenSearch Service      |
| DuckDB over CSV     | Athena + Glue over S3 (Parquet)|
| DynamoDB-Local      | Amazon DynamoDB                |
| Postgres (docker)   | Amazon RDS for PostgreSQL      |
| litellm -> any      | Amazon Bedrock (+ fallbacks)   |
| MinIO               | Amazon S3                      |
