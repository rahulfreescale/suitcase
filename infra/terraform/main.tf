# =============================================================================
# Suitcase - fully automated AWS deploy (sandbox-grade, destroy-friendly)
# Stands up: VPC, data stores, Redis queue, ECR, Fargate (API + workers),
# an ALB public URL, Secrets Manager, and a budget alarm.
# Single-AZ / minimal-hardening ON PURPOSE - apply, test, destroy same day.
# Driven by deploy.sh - you don't call terraform directly.
# =============================================================================
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
provider "aws" {
  region = var.region
}

variable "region" {
  default = "us-east-1"
}
variable "anthropic_key" {
  type      = string
  sensitive = true
}
variable "openai_key" {
  type      = string
  sensitive = true
}
variable "langfuse_public" {
  type    = string
  default = ""
}
variable "langfuse_secret" {
  type      = string
  default   = ""
  sensitive = true
}
variable "image_uri" {
  type    = string
  default = ""
}
variable "budget_usd" {
  default = "20"
}
variable "alert_email" {
  type    = string
  default = ""
}

data "aws_availability_zones" "az" {
  state = "available"
}
data "aws_caller_identity" "me" {}
locals {
  name = "suitcase"
  tags = {
    Project   = "suitcase"
    ManagedBy = "terraform"
    Ephemeral = "true"
  }
}

# ---------------- NETWORK ----------------
resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(local.tags, { Name = "${local.name}-vpc" })
}
resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = local.tags
}
resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.20.${count.index}.0/24"
  availability_zone       = data.aws_availability_zones.az.names[count.index]
  map_public_ip_on_launch = true
  tags                    = merge(local.tags, { Name = "${local.name}-public-${count.index}" })
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = local.tags
}
resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
resource "aws_security_group" "alb" {
  name_prefix = "${local.name}-alb-"
  vpc_id      = aws_vpc.main.id
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = local.tags
}
resource "aws_security_group" "svc" {
  name_prefix = "${local.name}-svc-"
  vpc_id      = aws_vpc.main.id
  ingress {
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    from_port = 0
    to_port   = 0
    protocol  = "-1"
    self      = true
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = local.tags
}

# ---------------- DATA STORES ----------------
resource "random_id" "sfx" {
  byte_length = 3
}
resource "aws_s3_bucket" "data_lake" {
  bucket        = "${local.name}-data-${var.region}-${random_id.sfx.hex}"
  force_destroy = true
  tags          = local.tags
}
resource "aws_s3_bucket" "athena_out" {
  bucket        = "${local.name}-athena-${var.region}-${random_id.sfx.hex}"
  force_destroy = true
  tags          = local.tags
}
resource "aws_dynamodb_table" "app_state" {
  name         = "${local.name}-app-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "thread_id"
  range_key    = "step_id"
  attribute {
    name = "thread_id"
    type = "S"
  }
  attribute {
    name = "step_id"
    type = "S"
  }
  tags = local.tags
}
resource "aws_opensearch_domain" "kb" {
  domain_name    = "${local.name}-kb"
  engine_version = "OpenSearch_2.17"
  cluster_config {
    instance_type  = "t3.small.search"
    instance_count = 1
  }
  ebs_options {
    ebs_enabled = true
    volume_size = 10
  }
  encrypt_at_rest {
    enabled = true
  }
  node_to_node_encryption {
    enabled = true
  }
  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }
  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = data.aws_caller_identity.me.account_id }
      Action    = "es:*"
      Resource  = "arn:aws:es:${var.region}:${data.aws_caller_identity.me.account_id}:domain/${local.name}-kb/*"
    }]
  })
  tags = local.tags
}
resource "random_password" "pg" {
  length  = 20
  special = false
}
resource "aws_db_subnet_group" "pg" {
  name       = "${local.name}-pg"
  subnet_ids = aws_subnet.public[*].id
  tags       = local.tags
}
resource "aws_db_instance" "checkpointer" {
  identifier             = "${local.name}-checkpointer"
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = "db.t3.micro"
  allocated_storage      = 20
  db_name                = "suitcase"
  username               = "suitcase"
  password               = random_password.pg.result
  db_subnet_group_name   = aws_db_subnet_group.pg.name
  vpc_security_group_ids  = [aws_security_group.svc.id]
  skip_final_snapshot    = true
  publicly_accessible    = false
  tags                   = local.tags
}
resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.public[*].id
}
resource "aws_elasticache_cluster" "redis" {
  cluster_id         = "${local.name}-redis"
  engine             = "redis"
  node_type          = "cache.t3.micro"
  num_cache_nodes    = 1
  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.svc.id]
  tags               = local.tags
}

# ---------------- SECRETS ----------------
resource "aws_secretsmanager_secret" "app" {
  name_prefix             = "${local.name}-secrets-"
  recovery_window_in_days = 0
  tags                    = local.tags
}
resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    ANTHROPIC_API_KEY   = var.anthropic_key
    OPENAI_API_KEY      = var.openai_key
    LANGFUSE_PUBLIC_KEY = var.langfuse_public
    LANGFUSE_SECRET_KEY = var.langfuse_secret
  })
}

# ---------------- REGISTRY ----------------
resource "aws_ecr_repository" "app" {
  name         = local.name
  force_delete = true
  tags         = local.tags
}

# ---------------- FARGATE ----------------
resource "aws_ecs_cluster" "main" {
  name = "${local.name}-cluster"
  tags = local.tags
}
resource "aws_iam_role" "task_exec" {
  name_prefix = "${local.name}-exec-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}
resource "aws_iam_role_policy_attachment" "exec" {
  role       = aws_iam_role.task_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "secrets" {
  role = aws_iam_role.task_exec.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_secretsmanager_secret.app.arn
    }]
  })
}
resource "aws_iam_role" "task" {
  name_prefix = "${local.name}-task-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}
resource "aws_iam_role_policy" "task_perms" {
  role = aws_iam_role.task.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:*"]
        Resource = aws_dynamodb_table.app_state.arn
      },
      {
        Effect   = "Allow"
        Action   = ["es:*"]
        Resource = "${aws_opensearch_domain.kb.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:*"]
        Resource = [aws_s3_bucket.data_lake.arn, "${aws_s3_bucket.data_lake.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "*"
      }
    ]
  })
}
resource "aws_cloudwatch_log_group" "app" {
  name_prefix       = "/ecs/${local.name}-"
  retention_in_days = 1
  tags              = local.tags
}

locals {
  container_env = [
    { name = "DEPLOY_PROFILE", value = "aws" },
    { name = "AWS_REGION", value = var.region },
    { name = "LLM_MODEL_CHAIN", value = "anthropic/claude-sonnet-4-6,gpt-4o" },
    { name = "LLM_FAST_MODEL", value = "anthropic/claude-haiku-4-5" },
    { name = "EMBED_MODEL", value = "text-embedding-3-small" },
    { name = "EMBED_DIM", value = "1536" },
    { name = "RERANK_BACKEND", value = "none" },
    { name = "LANGFUSE_HOST", value = "https://us.cloud.langfuse.com" },
    { name = "OPENSEARCH_HOST", value = aws_opensearch_domain.kb.endpoint },
    { name = "OPENSEARCH_PORT", value = "443" },
    { name = "OPENSEARCH_USE_SSL", value = "true" },
    { name = "OPENSEARCH_USE_AWS_AUTH", value = "true" },
    { name = "DYNAMODB_TABLE", value = aws_dynamodb_table.app_state.name },
    { name = "DYNAMODB_ENDPOINT", value = "" },
    { name = "COGNITO_USER_POOL_ID", value = aws_cognito_user_pool.users.id },
    { name = "COGNITO_CLIENT_ID", value = aws_cognito_user_pool_client.web.id },
    { name = "REDIS_URL", value = "redis://${aws_elasticache_cluster.redis.cache_nodes[0].address}:6379/0" },
    { name = "POSTGRES_DSN", value = "postgresql://suitcase:${random_password.pg.result}@${aws_db_instance.checkpointer.address}:5432/suitcase" },
  ]
  container_secrets = [
    { name = "ANTHROPIC_API_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:ANTHROPIC_API_KEY::" },
    { name = "OPENAI_API_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:OPENAI_API_KEY::" },
    { name = "LANGFUSE_PUBLIC_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:LANGFUSE_PUBLIC_KEY::" },
    { name = "LANGFUSE_SECRET_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:LANGFUSE_SECRET_KEY::" },
  ]
}
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.task_exec.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([{
    name         = "api"
    image        = var.image_uri
    command      = ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
    portMappings = [{ containerPort = 8080 }]
    environment  = local.container_env
    secrets      = local.container_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }
  }])
  tags = local.tags
}
resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.svc.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8080
  }
  depends_on = [aws_lb_listener.http]
  tags       = local.tags
}
resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.task_exec.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([{
    name        = "worker"
    image       = var.image_uri
    command     = ["python", "-m", "app.worker"]
    environment = local.container_env
    secrets     = local.container_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
  tags = local.tags
}
resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.svc.id]
    assign_public_ip = true
  }
  tags = local.tags
}

# ---------------- LOAD BALANCER ----------------
resource "aws_lb" "app" {
  name_prefix        = "sc-"
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]
  idle_timeout       = 300
  tags               = local.tags
}
resource "aws_lb_target_group" "api" {
  name_prefix = "sc-"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"
  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
  tags = local.tags
}
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# ---------------- BUDGET ALARM ----------------
resource "aws_budgets_budget" "cap" {
  name         = "${local.name}-budget"
  budget_type  = "COST"
  limit_amount = var.budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  dynamic "notification" {
    for_each = var.alert_email == "" ? [] : [1]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = 50
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }
  dynamic "notification" {
    for_each = var.alert_email == "" ? [] : [1]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = 90
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}

# ---------------- OUTPUTS ----------------
output "app_url" {
  value = "http://${aws_lb.app.dns_name}"
}
output "ecr_repo_url" {
  value = aws_ecr_repository.app.repository_url
}
output "opensearch_host" {
  value = aws_opensearch_domain.kb.endpoint
}
output "cluster_name" {
  value = aws_ecs_cluster.main.name
}
output "region" {
  value = var.region
}

# =============================================================================
# COGNITO — real login. The app (on Fargate) verifies the JWTs Cognito issues.
# No domain/cert needed: Cognito hosts the login page over its own HTTPS, and
# the app checks tokens itself over plain HTTP behind the ALB.
# =============================================================================
variable "test_user_email" {
  type    = string
  default = "rahulfreescale@gmail.com"
}

resource "aws_cognito_user_pool" "users" {
  name = "${local.name}-users"
  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }
  auto_verified_attributes = ["email"]
  tags                     = local.tags
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "${local.name}-web"
  user_pool_id = aws_cognito_user_pool.users.id
  # hosted UI login: authorization-code flow, tokens for the browser
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["email", "openid", "profile"]
  allowed_oauth_flows_user_pool_client = true
  supported_identity_providers         = ["COGNITO"]
  # after login, Cognito redirects back to the app URL with the token
  callback_urls = ["http://localhost:8080/", "https://localhost/"]
  logout_urls   = ["http://localhost:8080/"]
  explicit_auth_flows = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
  generate_secret     = false
}

# the AWS-hosted login page lives at this domain
resource "aws_cognito_user_pool_domain" "login" {
  domain       = "${local.name}-${random_id.sfx.hex}"
  user_pool_id = aws_cognito_user_pool.users.id
}

# seed the one test user so you can log in immediately
resource "aws_cognito_user" "test" {
  user_pool_id = aws_cognito_user_pool.users.id
  username     = var.test_user_email
  attributes = {
    email          = var.test_user_email
    email_verified = "true"
  }
  # Cognito emails a temporary password to this address on create
}

output "cognito_pool_id" {
  value = aws_cognito_user_pool.users.id
}
output "cognito_client_id" {
  value = aws_cognito_user_pool_client.web.id
}
output "cognito_login_url" {
  value = "Get a token via CLI (no browser redirect needed): aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH --client-id ${aws_cognito_user_pool_client.web.id} --auth-parameters USERNAME=${var.test_user_email},PASSWORD=<your-password> --region ${var.region}"
}
