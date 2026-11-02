# The app and the weekly ingest job run as AWS Lambda functions from container
# images. Nothing runs, and almost nothing is billed, between requests. The
# functions, their URL, CloudFront and the schedule exist once image_tag is set,
# because Lambda needs a pushed image to create a function.

# ---------------------------------------------------------------- images
# One image, log group and function each: the app, the weekly ingest, and the
# scheduled evaluation (infra/eval.tf).

locals {
  functions = ["app", "ingest", "eval"]
}

resource "aws_ecr_repository" "this" {
  for_each = toset(local.functions)

  name                 = "${local.name}-${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# Lambda pulls the image with its own service principal, limited to this
# project's functions in this account.
data "aws_iam_policy_document" "ecr_lambda_pull" {
  statement {
    sid     = "LambdaPull"
    actions = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:sourceArn"
      values   = ["arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${local.name}-*"]
    }
  }
}

resource "aws_ecr_repository_policy" "lambda_pull" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name
  policy     = data.aws_iam_policy_document.ecr_lambda_pull.json
}

# ---------------------------------------------------------------- logs

resource "aws_cloudwatch_log_group" "this" {
  for_each          = toset(local.functions)
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = var.log_retention_days
}

locals {
  function_arn = { for k in local.functions : k => "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${local.name}-${k}" }

  azure_env = {
    AZURE_OPENAI_ENDPOINT             = azurerm_cognitive_account.openai.endpoint
    AZURE_OPENAI_CHAT_DEPLOYMENT      = azurerm_cognitive_deployment.chat.name
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT = azurerm_cognitive_deployment.embedding.name
    SEARCH_ENDPOINT                   = "https://${azurerm_search_service.search.name}.search.windows.net"
    SEARCH_INDEX                      = local.search_index_name
    AZURE_OPENAI_API_KEY_SECRET_ARN   = aws_secretsmanager_secret.this["azure-openai-key"].arn
  }

  # Secrets appear only as ARNs; app/settings.py reads the values from Secrets
  # Manager at start-up, with the function's role.
  # PUBLIC_BASE_URL isn't set: it would be the CloudFront address, and CloudFront
  # depends on this function. The admin sign-in (Phase 3) takes it from the
  # forwarded host instead.
  app_env = merge(local.azure_env, {
    SEARCH_QUERY_KEY_SECRET_ARN    = aws_secretsmanager_secret.this["search-query-key"].arn
    ENTRA_CLIENT_SECRET_SECRET_ARN = aws_secretsmanager_secret.this["entra-client-secret"].arn
    SESSION_SECRET_SECRET_ARN      = aws_secretsmanager_secret.this["session-secret"].arn
    BEDROCK_REGION                 = var.aws_region
    BEDROCK_MODEL_ID               = var.bedrock_inference_profile_id
    FEEDBACK_TABLE                 = aws_dynamodb_table.feedback.name
    FEEDBACK_RETENTION_DAYS        = tostring(var.feedback_retention_days)
    ENTRA_TENANT_ID                = data.azuread_client_config.current.tenant_id
    ENTRA_CLIENT_ID                = azuread_application.admin.client_id
    ENTRA_ADMIN_ROLE               = "PolicyDesk.Admin"
    EVAL_RECORDS_BUCKET            = aws_s3_bucket.eval_records.bucket
    OTEL_SERVICE_NAME              = "${local.name}-app"
    OTEL_TRACES_EXPORTER           = "otlp"
    OTEL_EXPORTER_OTLP_ENDPOINT    = "https://xray.${var.aws_region}.amazonaws.com"
  })

  ingest_env = merge(local.azure_env, {
    SEARCH_ADMIN_KEY_SECRET_ARN = aws_secretsmanager_secret.this["search-admin-key"].arn
    EVAL_RECORDS_BUCKET         = aws_s3_bucket.eval_records.bucket
  })
}

# ---------------------------------------------------------------- app

resource "aws_lambda_function" "app" {
  count = local.deployed ? 1 : 0

  function_name = "${local.name}-app"
  description   = "Policy Desk: the page and POST /ask"
  role          = aws_iam_role.app.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.this["app"].repository_url}:${var.image_tag}"
  architectures = ["arm64"]
  memory_size   = var.app_memory_mb
  timeout       = 60

  # Caps concurrent requests, which caps what a flood of requests can cost.
  # -1 means no cap; new accounts may not have room to reserve any.
  reserved_concurrent_executions = var.app_reserved_concurrency

  environment {
    variables = local.app_env
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.this["app"].name
  }

  tracing_config {
    mode = "Active"
  }

  # The image is set here only when the function is created: after that, the pipeline
  # (or make release) deploys new images, and an apply mustn't put the old one back.
  lifecycle {
    ignore_changes = [image_uri]
  }

  # X-Ray's OTLP endpoint takes spans only once Transaction Search is on.
  depends_on = [
    aws_iam_role_policy.app,
    aws_ecr_repository_policy.lambda_pull,
    aws_xray_trace_segment_destination.this,
  ]
}

# A new account's quota of 10 concurrent executions caps the app by itself, and
# Lambda refuses any reservation that would leave fewer than 100 unreserved (or
# all of a quota of 10). AWS raises new accounts' quotas as they're used, which
# would lift the cap silently, so plan warns once it has.
check "app_concurrency_capped" {
  data "aws_servicequotas_service_quota" "lambda_concurrency" {
    service_code = "lambda"
    quota_code   = "L-B99A9384" # Concurrent executions
  }

  assert {
    condition     = var.app_reserved_concurrency >= 0 || data.aws_servicequotas_service_quota.lambda_concurrency.value <= 10
    error_message = "This account's Lambda concurrency quota is now ${data.aws_servicequotas_service_quota.lambda_concurrency.value}, so it no longer caps the app at 10 requests at once. Set app_reserved_concurrency (for example 3) in terraform.tfvars."
  }
}

# Only CloudFront can call this URL: requests must be signed (AWS_IAM), and the
# only principal allowed to sign them is this distribution (see edge.tf).
resource "aws_lambda_function_url" "app" {
  count              = local.deployed ? 1 : 0
  function_name      = aws_lambda_function.app[0].function_name
  authorization_type = "AWS_IAM"
  invoke_mode        = "BUFFERED"
}

# ---------------------------------------------------------------- weekly ingest

resource "aws_lambda_function" "ingest" {
  count = local.deployed ? 1 : 0

  function_name = "${local.name}-ingest"
  description   = "Policy Desk: weekly fetch, chunk, embed and index"
  role          = aws_iam_role.ingest.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.this["ingest"].repository_url}:${var.image_tag}"
  architectures = ["arm64"]
  memory_size   = var.ingest_memory_mb
  # A full run takes about 90 seconds, mostly the crawl delay between requests,
  # so weekly runs can't overlap.
  timeout = 900

  environment {
    variables = local.ingest_env
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.this["ingest"].name
  }

  tracing_config {
    mode = "Active"
  }

  depends_on = [aws_iam_role_policy.ingest, aws_ecr_repository_policy.lambda_pull]

  # Set at creation only; deploys update it (see the app function).
  lifecycle {
    ignore_changes = [image_uri]
  }
}

# A failed run isn't retried: Lambda would otherwise run it twice more within
# minutes, re-crawling every source. The ingest_failed alarm emails instead, and
# next week's run starts afresh.
resource "aws_lambda_function_event_invoke_config" "ingest" {
  count                  = local.deployed ? 1 : 0
  function_name          = aws_lambda_function.ingest[0].function_name
  maximum_retry_attempts = 0
}

resource "aws_scheduler_schedule" "ingest" {
  count = local.deployed ? 1 : 0

  name                         = "${local.name}-weekly-ingest"
  schedule_expression          = var.ingest_schedule
  schedule_expression_timezone = "Australia/Sydney"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.ingest[0].arn
    role_arn = aws_iam_role.scheduler.arn

    retry_policy {
      maximum_retry_attempts = 1
    }
  }
}
