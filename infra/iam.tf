# Every role is scoped to named resources. The only wildcards are actions that
# have no resource-level permissions: X-Ray trace upload, ECR sign-in, and
# reading several secrets in one call, which still needs GetSecretValue on each.

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# Logs and traces, for each function's own log group.
data "aws_iam_policy_document" "function_basics" {
  for_each = toset(local.functions)

  statement {
    sid       = "OwnLogGroup"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this[each.key].arn}:*"]
  }

  statement {
    sid       = "Traces"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

# BatchGetSecretValue can't be limited to named secrets, so it's granted on "*".
# Secrets Manager returns only the secrets the role may also GetSecretValue, so
# each role's GetSecretValue list below is what decides access.
data "aws_iam_policy_document" "batch_get_secrets" {
  statement {
    sid       = "BatchRead"
    actions   = ["secretsmanager:BatchGetSecretValue"]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "bedrock_invoke" {
  statement {
    sid       = "InvokeThroughProfile"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = [local.bedrock_profile_arn]
  }

  statement {
    sid       = "InvokeModelInProfileRegions"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = local.bedrock_model_arns

    condition {
      test     = "StringLike"
      variable = "bedrock:InferenceProfileArn"
      values   = [local.bedrock_profile_arn]
    }
  }
}

# ---------------------------------------------------------------- app role
# What the app may do: read its four secrets, use the feedback table, call
# Bedrock through the AU profile.

data "aws_iam_policy_document" "app" {
  source_policy_documents = [
    data.aws_iam_policy_document.function_basics["app"].json,
    data.aws_iam_policy_document.batch_get_secrets.json,
    data.aws_iam_policy_document.bedrock_invoke.json,
  ]

  # The app's own spans, sent to X-Ray's OTLP endpoint. Neither action can be
  # limited to named resources.
  statement {
    sid       = "AppSpans"
    actions   = ["xray:PutSpans", "xray:PutSpansForIndexing"]
    resources = ["*"]
  }

  statement {
    sid     = "AppSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.this["azure-openai-key"].arn,
      aws_secretsmanager_secret.this["search-query-key"].arn,
      aws_secretsmanager_secret.this["entra-client-secret"].arn,
      aws_secretsmanager_secret.this["session-secret"].arn,
    ]
  }

  statement {
    sid = "FeedbackTable"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.feedback.arn,
      "${aws_dynamodb_table.feedback.arn}/index/by_status",
    ]
  }

  # The status page: the snapshot the scheduled evaluation writes, and the states of
  # this service's alarms. Read only.
  statement {
    sid       = "StatusSnapshot"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.eval_records.arn}/status/snapshot.json"]
  }

  # Can't be limited to named alarms: AWS refused the alarm-scoped form (2 Nov 2026).
  # It reads alarms' names and states only, and the page shows only this service's.
  statement {
    sid       = "StatusAlarms"
    actions   = ["cloudwatch:DescribeAlarms"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "app" {
  name               = "${local.name}-app"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "app" {
  name   = "app"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app.json
}

# ---------------------------------------------------------------- ingest role
# Read the embedding key and the search admin key, and record each run's changes for
# the drift checks; nothing else.

data "aws_iam_policy_document" "ingest" {
  source_policy_documents = [
    data.aws_iam_policy_document.function_basics["ingest"].json,
    data.aws_iam_policy_document.batch_get_secrets.json,
  ]

  statement {
    sid     = "IngestSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.this["azure-openai-key"].arn,
      aws_secretsmanager_secret.this["search-admin-key"].arn,
    ]
  }

  statement {
    sid       = "IngestRecords"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.eval_records.arn}/ingest/*"]
  }
}

resource "aws_iam_role" "ingest" {
  name               = "${local.name}-ingest"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "ingest" {
  name   = "ingest"
  role   = aws_iam_role.ingest.id
  policy = data.aws_iam_policy_document.ingest.json
}

# ---------------------------------------------------------------- scheduler role
# EventBridge Scheduler may invoke the ingest and evaluation functions and nothing else.

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "scheduler.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [local.function_arn["ingest"], local.function_arn["eval"]]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "run-scheduled"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

# ---------------------------------------------------------------- GitHub Actions
# OIDC: workflows get short-lived credentials, so no cloud keys are stored
# in GitHub. Two roles:
#   ci     - main only, the evaluation gate: read the model keys, call Bedrock,
#            record the release run
#   deploy - main only: push the three images, point the functions at them, and
#            start the post-deploy canary

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "github_ci_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      # main only, where the evaluation gate runs. Pull requests and other
      # branches can't assume this role, so they can't read the model keys.
      values = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

data "aws_iam_policy_document" "github_deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

# The evaluation gate runs the app's image on the runner, so it needs what the app
# needs to answer (the two keys, read in a batch as the app reads them, and Bedrock
# for the failover), and records the run as a release run.
data "aws_iam_policy_document" "github_ci" {
  source_policy_documents = [
    data.aws_iam_policy_document.bedrock_invoke.json,
    data.aws_iam_policy_document.batch_get_secrets.json,
  ]

  statement {
    sid     = "EvaluationKeys"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.this["azure-openai-key"].arn,
      aws_secretsmanager_secret.this["search-query-key"].arn,
    ]
  }

  statement {
    sid       = "ReleaseRecords"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.eval_records.arn}/runs/release/*"]
  }
}

resource "aws_iam_role" "github_ci" {
  name               = "${local.name}-github-ci"
  assume_role_policy = data.aws_iam_policy_document.github_ci_trust.json
}

resource "aws_iam_role_policy" "github_ci" {
  name   = "evaluate"
  role   = aws_iam_role.github_ci.id
  policy = data.aws_iam_policy_document.github_ci.json
}

data "aws_iam_policy_document" "github_deploy" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:DescribeImageScanFindings",
    ]
    resources = [for r in aws_ecr_repository.this : r.arn]
  }

  statement {
    sid = "UpdateFunctions"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:PublishVersion",
    ]
    resources = values(local.function_arn)
  }

  # After a deploy, a canary against production, recorded as a post-deploy run.
  statement {
    sid       = "PostDeployCheck"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.function_arn["eval"]]
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = "${local.name}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_deploy_trust.json
}

resource "aws_iam_role_policy" "github_deploy" {
  name   = "deploy"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}
