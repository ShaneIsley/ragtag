# The scheduled evaluation: the golden set's gate, run against production by
# EventBridge Scheduler (evals/lambda_handler.py). Nightly, judge calibration and a
# 13-case canary; weekly, an hour after the ingest, the full set, a standby check of
# the failover model, and the drift checks. Each run's record and results go to S3; a
# run that fails emails through the eval_failed alarm, and drift through the drift
# alarm. About US$2.50 a month in model calls.

# ---------------------------------------------------------------- records

resource "aws_s3_bucket" "eval_records" {
  bucket        = "${local.name}-eval-records-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "eval_records" {
  bucket                  = aws_s3_bucket.eval_records.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "eval_records" {
  bucket = aws_s3_bucket.eval_records.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "eval_records" {
  bucket = aws_s3_bucket.eval_records.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Records are a few kilobytes and kept; each run's full results are kept 90 days.
resource "aws_s3_bucket_lifecycle_configuration" "eval_records" {
  bucket = aws_s3_bucket.eval_records.id

  dynamic "rule" {
    for_each = toset(["release", "post-deploy", "monitor", "experiment", "drill", "calibration"])
    content {
      id     = "expire-${rule.value}-results"
      status = "Enabled"
      filter {
        prefix = "runs/${rule.value}/results/"
      }
      expiration {
        days = 90
      }
    }
  }
}

data "aws_iam_policy_document" "eval_records_tls" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.eval_records.arn, "${aws_s3_bucket.eval_records.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "eval_records" {
  bucket = aws_s3_bucket.eval_records.id
  policy = data.aws_iam_policy_document.eval_records_tls.json

  depends_on = [aws_s3_bucket_public_access_block.eval_records]
}

# ---------------------------------------------------------------- role
# Read the Azure OpenAI key (the graders and the facts judge use the chat
# deployment), write monitor and calibration records, and hold the run lock. For the
# drift checks and the status snapshot: read the records and the ingest's, query the
# app's spans, write the drift report and the snapshot, and count drift's signals. Nothing else: the runs reach the app
# through CloudFront like any visitor, and Bedrock only through the app.

data "aws_iam_policy_document" "eval" {
  source_policy_documents = [
    data.aws_iam_policy_document.function_basics["eval"].json,
    data.aws_iam_policy_document.batch_get_secrets.json,
  ]

  statement {
    sid       = "EvalSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.this["azure-openai-key"].arn]
  }

  statement {
    sid     = "MonitorRecords"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.eval_records.arn}/runs/monitor/*",
      "${aws_s3_bucket.eval_records.arn}/runs/post-deploy/*",
      "${aws_s3_bucket.eval_records.arn}/runs/calibration/*",
    ]
  }

  statement {
    sid       = "DriftReadRecords"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.eval_records.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [for p in concat(local.drift_reads, ["drift/"]) : "${p}*"]
    }
  }

  statement {
    sid       = "DriftReadObjects"
    actions   = ["s3:GetObject"]
    resources = [for p in concat(local.drift_reads, ["drift/"]) : "${aws_s3_bucket.eval_records.arn}/${p}*"]
  }

  # The drift report, and the status page's snapshot.
  statement {
    sid     = "Reports"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.eval_records.arn}/drift/*",
      "${aws_s3_bucket.eval_records.arn}/status/*",
    ]
  }

  statement {
    sid     = "DriftSpans"
    actions = ["logs:StartQuery"]
    resources = [
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:aws/spans",
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:aws/spans:*",
    ]
  }

  # Neither can be limited to a resource: query results by their ID, and metrics by
  # their namespace (the condition).
  statement {
    sid       = "DriftQueryResults"
    actions   = ["logs:GetQueryResults"]
    resources = ["*"]
  }

  statement {
    sid       = "DriftSignals"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["PolicyDesk"]
    }
  }

  # One run at a time (evals/lambda_handler.py): the lock object only.
  statement {
    sid       = "RunLock"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.eval_records.arn}/lock/eval-running.json"]
  }
}

resource "aws_iam_role" "eval" {
  name               = "${local.name}-eval"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "eval" {
  name   = "eval"
  role   = aws_iam_role.eval.id
  policy = data.aws_iam_policy_document.eval.json
}

# ---------------------------------------------------------------- function

resource "aws_lambda_function" "eval" {
  count = local.deployed ? 1 : 0

  function_name = "${local.name}-eval"
  description   = "Policy Desk: the evaluation gate against production, on a schedule"
  role          = aws_iam_role.eval.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.this["eval"].repository_url}:${var.image_tag}"
  architectures = ["arm64"]
  # promptfoo runs each case's checks three at a time, each in a new Python
  # process; more memory brings more vCPU for them (about 1.7 at this size). 3008 MB
  # is this account's Lambda maximum until a quota increase. Within Lambda's free
  # allowance.
  memory_size = 3008
  # A full run took 10 to 11 minutes locally, one case at a time; on Lambda the
  # first one didn't finish in 14 (docs/evaluation.md, The first weekly runs).
  timeout = 900

  ephemeral_storage {
    size = 1024
  }

  environment {
    variables = merge(local.azure_env, {
      EVAL_TARGET_URL     = "https://${aws_cloudfront_distribution.app[0].domain_name}"
      EVAL_RECORDS_BUCKET = aws_s3_bucket.eval_records.bucket
    })
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.this["eval"].name
  }

  # Set at creation only; deploys update it (see the app function).
  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [aws_iam_role_policy.eval, aws_ecr_repository_policy.lambda_pull]
}

# A failed run isn't retried: a retry would spend the same again on the same
# answer. The alarm emails instead.
resource "aws_lambda_function_event_invoke_config" "eval" {
  count                  = local.deployed ? 1 : 0
  function_name          = aws_lambda_function.eval[0].function_name
  maximum_retry_attempts = 0
}

# ---------------------------------------------------------------- schedules
# Sydney time. Calibration runs first each night, so a judge that has drifted is
# known before the canary is judged by it. The ingest runs Mondays at 3 am; the full
# run follows at 4, so a source change that breaks an answer shows the same morning,
# and the standby check at 4:30, clear of the full run's use of the model's rate limit.
# The drift checks at 5 read what all of them recorded.

locals {
  # What the drift checks read: the production runs, calibration, and the ingest's
  # records of what it changed.
  drift_reads = ["runs/monitor/", "runs/post-deploy/", "runs/calibration/", "ingest/"]

  eval_schedules = {
    calibration = "cron(15 2 * * ? *)"
    canary      = "cron(30 2 * * ? *)"
    full        = "cron(0 4 ? * MON *)"
    standby     = "cron(30 4 ? * MON *)"
    drift       = "cron(0 5 ? * MON *)"
  }
}

resource "aws_scheduler_schedule" "eval" {
  for_each = local.deployed ? local.eval_schedules : {}

  name                         = "${local.name}-eval-${each.key}"
  schedule_expression          = each.value
  schedule_expression_timezone = "Australia/Sydney"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.eval[0].arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ suite = each.key })

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "eval_failed" {
  count               = local.deployed ? 1 : 0
  alarm_name          = "${local.name}-eval-failed"
  alarm_description   = "A scheduled evaluation run failed the gate or judge calibration, or didn't finish. Its record in the eval records bucket (runs/monitor/ or runs/calibration/) says why; the function's log has the output."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = "${local.name}-eval" }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# The drift checks (evals/drift.py) count the signals someone should act on: a new
# model version, changed sources with golden cases to check, a missing ingest record,
# or a large shift in what visitors ask.
resource "aws_cloudwatch_metric_alarm" "drift" {
  count               = local.deployed ? 1 : 0
  alarm_name          = "${local.name}-drift"
  alarm_description   = "The weekly drift checks found something to act on: a new model version, sources that changed under golden cases, a missing ingest record, or a large shift in what visitors ask. drift/latest.json in the eval records bucket says what and which cases."
  namespace           = "PolicyDesk"
  metric_name         = "DriftSignals"
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
