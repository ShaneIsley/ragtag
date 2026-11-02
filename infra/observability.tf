resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------- spans
# CloudWatch Transaction Search: X-Ray stores every span, Lambda's own and the app's,
# as structured logs in aws/spans, searchable by trace ID, and indexes 1% of them as
# trace summaries (the free default). It's an account-wide setting for the region.
# The app's spans hold no question or answer text and no client address
# (app/telemetry.py); they're kept as long as the functions' logs.
#
# Names starting with aws/ are reserved, so only X-Ray can create aws/spans, which it
# does when Transaction Search is turned on. Terraform can't manage that log group,
# so spans_retention sets its retention with the AWS CLI on the machine running
# apply, once X-Ray has made it.

locals {
  account_id = data.aws_caller_identity.current.account_id
  log_group_arn = {
    for k, name in { spans = "aws/spans", signals = "/aws/application-signals/data" } :
    k => "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:${name}"
  }
}

data "aws_iam_policy_document" "xray_spans" {
  statement {
    sid       = "TransactionSearchXRayAccess"
    actions   = ["logs:PutLogEvents"]
    resources = [for arn in values(local.log_group_arn) : "${arn}:*"]

    principals {
      type        = "Service"
      identifiers = ["xray.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:xray:${var.aws_region}:${local.account_id}:*"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "xray_spans" {
  policy_name     = "${local.name}-transaction-search"
  policy_document = data.aws_iam_policy_document.xray_spans.json
}

resource "aws_xray_trace_segment_destination" "this" {
  destination = "CloudWatchLogs"

  depends_on = [aws_cloudwatch_log_resource_policy.xray_spans]
}

# Waits up to two minutes for X-Ray to create the log group. Runs again only if the
# retention changes; a retention changed outside Terraform isn't detected.
resource "terraform_data" "spans_retention" {
  triggers_replace = [var.log_retention_days, aws_xray_trace_segment_destination.this.destination]

  provisioner "local-exec" {
    command = <<-EOT
      for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
        aws logs put-retention-policy --region ${var.aws_region} \
          --log-group-name aws/spans --retention-in-days ${var.log_retention_days} && exit 0
        sleep 10
      done
      echo "aws/spans doesn't exist yet: is Transaction Search on?" >&2
      exit 1
    EOT
  }
}

# Alarms and the dashboard watch the two functions, so they exist once
# image_tag is set. Lambda metrics cover the app end to end: CloudFront adds
# nothing that can fail on its own except its own outages.

locals {
  app_fn    = "${local.name}-app"
  ingest_fn = "${local.name}-ingest"
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  count               = local.deployed ? 1 : 0
  alarm_name          = "${local.name}-app-errors"
  alarm_description   = "The app failed more than 5 requests in 5 minutes (unhandled errors or timeouts)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = local.app_fn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "throttles" {
  count               = local.deployed ? 1 : 0
  alarm_name          = "${local.name}-app-throttled"
  alarm_description   = "Requests were turned away because the app hit its concurrency limit."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = local.app_fn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "slow" {
  count               = local.deployed ? 1 : 0
  alarm_name          = "${local.name}-app-slow"
  alarm_description   = "p95 app duration above ${var.latency_alarm_seconds}s for 15 minutes."
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  dimensions          = { FunctionName = local.app_fn }
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.latency_alarm_seconds * 1000
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "ingest_failed" {
  count               = local.deployed ? 1 : 0
  alarm_name          = "${local.name}-ingest-failed"
  alarm_description   = "The weekly ingest failed: a source couldn't be fetched or indexed, or the run errored."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = local.ingest_fn }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# ---------------------------------------------------------------- answers, from spans
# Each answer's spans (app/telemetry.py) give the answer-level picture: outcomes,
# which model answered, tokens and cost, time per step, search coverage, and the
# privacy and safety controls at work. The dashboard queries aws/spans with Logs
# Insights; the alarms below count spans with metric filters. Neither sees question
# or answer text, because spans don't hold any.

locals {
  # List prices in US$ per million tokens, for the cost panel only.
  price = {
    chat_in    = 0.25 # gpt-5-mini, Azure OpenAI global
    chat_out   = 2
    claude_in  = 1 # Claude Haiku 4.5 on Bedrock
    claude_out = 5
    embed_in   = 0.02 # text-embedding-3-small
  }
  # Each pattern matches a term found on one kind of span only.
  span_metrics = {
    Answers        = "\"app.requested_provider\"" # every answer span
    Declines       = "\"app.decline_reason\""     # answer spans that declined
    Failovers      = "\"app.failover_reason\""    # model calls that failed over
    BlockedAnswers = "\"answer_filtered\""        # declined because Azure blocked the answer
  }
  # app/answer.py MIN_SIMILARITY
  decline_below = "0.40"
}

resource "aws_cloudwatch_log_metric_filter" "spans" {
  for_each       = local.span_metrics
  name           = "${local.name}-${lower(each.key)}"
  log_group_name = "aws/spans"
  pattern        = each.value

  metric_transformation {
    name      = each.key
    namespace = "PolicyDesk"
    value     = "1"
    unit      = "Count"
  }

  depends_on = [terraform_data.spans_retention]
}

# Most questions declined usually means search has broken or the Free-tier index
# has gone, which still returns HTTP 200 and so trips no other alarm.
resource "aws_cloudwatch_metric_alarm" "mostly_declined" {
  count               = local.deployed ? 1 : 0
  alarm_name          = "${local.name}-mostly-declined"
  alarm_description   = "Over 80% of at least 5 questions in 15 minutes were declined. Check that the search index exists and has passages (the runbook's index rebuild)."
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0.8
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  metric_query {
    id          = "share"
    expression  = "IF(answers >= 5, FILL(declines, 0) / answers, 0)"
    label       = "Share of questions declined"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = { answers = "Answers", declines = "Declines" }
    content {
      id = metric_query.key
      metric {
        namespace   = "PolicyDesk"
        metric_name = metric_query.value
        period      = 900
        stat        = "Sum"
      }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "failovers" {
  count               = local.deployed ? 1 : 0
  alarm_name          = "${local.name}-failovers"
  alarm_description   = "Two or more answers in 15 minutes came from Bedrock because Azure OpenAI didn't respond. Check Azure OpenAI's status and the deployment's rate limit."
  namespace           = "PolicyDesk"
  metric_name         = "Failovers"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "blocked_answer" {
  count               = local.deployed ? 1 : 0
  alarm_name          = "${local.name}-blocked-answer"
  alarm_description   = "Azure's content filter blocked an answer the model wrote. Find its trace by time on the dashboard and check which passages it used."
  namespace           = "PolicyDesk"
  metric_name         = "BlockedAnswers"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

locals {
  # Logs Insights queries over the app's spans, one per panel.
  answer_panels = [
    {
      title = "Questions by outcome"
      view  = "bar"
      query = <<-EOT
        filter name = "answer"
        | fields coalesce(attributes.app.decline_reason, "answered") as outcome
        | stats count(*) as questions by outcome
        | sort questions desc
      EOT
    },
    {
      title = "Answered and declined, by hour"
      view  = "timeSeries"
      query = <<-EOT
        filter name = "answer"
        | fields if(ispresent(attributes.app.decline_reason), 0, 1) as answered, if(ispresent(attributes.app.decline_reason), 1, 0) as declined
        | stats sum(answered) as answers, sum(declined) as declines by bin(1h)
      EOT
    },
    {
      title = "Which model answered, and which was asked for"
      view  = "table"
      query = <<-EOT
        filter name = "answer"
        | fields attributes.app.provider as answered_by, attributes.app.requested_provider as asked_for
        | stats count(*) as questions by answered_by, asked_for
      EOT
    },
    {
      title = "Failovers to Bedrock, by reason"
      view  = "table"
      query = <<-EOT
        filter ispresent(attributes.app.failover_reason)
        | fields attributes.app.failover_reason as reason
        | stats count(*) as failovers by reason
      EOT
    },
    {
      title = "Tokens and cost by day (US$, list prices)"
      view  = "table"
      query = <<-EOT
        filter attributes.gen_ai.operation.name in ["chat", "embeddings"]
        | fields attributes.gen_ai.usage.input_tokens as input, coalesce(attributes.gen_ai.usage.output_tokens, 0) as output, attributes.gen_ai.provider.name as provider, attributes.gen_ai.operation.name as operation
        | fields case(operation = "embeddings", input * ${local.price.embed_in}, provider = "aws.bedrock", input * ${local.price.claude_in} + output * ${local.price.claude_out}, input * ${local.price.chat_in} + output * ${local.price.chat_out}) / 1000000 as usd
        | stats count_distinct(traceId) as requests, sum(input) as input_tokens, sum(output) as output_tokens, sum(usd) as usd by bin(1d)
      EOT
    },
    {
      title = "Busiest minutes on Azure OpenAI (limit ${var.chat_capacity * 1000} tokens and ${var.chat_capacity} requests a minute)"
      view  = "table"
      query = <<-EOT
        filter attributes.gen_ai.provider.name = "azure.ai.openai" and attributes.gen_ai.operation.name = "chat"
        | fields attributes.gen_ai.usage.input_tokens + attributes.gen_ai.usage.output_tokens as tokens
        | stats sum(tokens) as tokens_per_minute, count(*) as requests_per_minute by bin(1m)
        | sort tokens_per_minute desc
        | limit 5
      EOT
    },
    {
      title = "Time per step (ms)"
      view  = "table"
      query = <<-EOT
        filter scope.name = "policy_desk"
        | fields durationNano / 1000000 as ms
        | stats count(*) as spans, pct(ms, 50) as p50_ms, pct(ms, 95) as p95_ms by name
        | sort p50_ms desc
      EOT
    },
    {
      title = "Cold starts by day (s)"
      view  = "table"
      query = <<-EOT
        filter name = "Init/LambdaExecutionEnvironment" and resource.attributes.faas.name = "${local.app_fn}"
        | fields durationNano / 1000000000 as seconds
        | stats count(*) as cold_starts, avg(seconds) as average_s, max(seconds) as longest_s by bin(1d)
      EOT
    },
    {
      title = "Search coverage: questions by best match (declined below ${local.decline_below})"
      view  = "bar"
      query = <<-EOT
        filter name = "search"
        | fields floor(attributes.app.top_similarity * 20) / 20 as best_match
        | stats count(*) as questions by best_match
        | sort best_match asc
      EOT
    },
    {
      title = "Sources retrieved"
      view  = "bar"
      query = <<-EOT
        filter name = "search"
        | fields jsonParse(@message) as span
        | unnest span.attributes.`app.source_ids` into source
        | stats count(*) as questions by source
        | sort questions desc
      EOT
    },
    {
      title = "Sources cited"
      view  = "bar"
      query = <<-EOT
        filter name = "answer" and not ispresent(attributes.app.decline_reason)
        | fields jsonParse(@message) as span
        | unnest span.attributes.`app.cited_sources` into source
        | stats count(*) as answers by source
        | sort answers desc
      EOT
    },
    {
      title = "Details removed from questions, by kind"
      view  = "bar"
      query = <<-EOT
        filter name = "mask"
        | fields jsonParse(@message) as span
        | unnest span.attributes.`app.masked` into removed
        | stats count(*) as questions by removed
        | sort questions desc
      EOT
    },
    {
      title = "Content safety ratings above safe, on questions"
      view  = "table"
      query = <<-EOT
        filter name = "answer"
        | fields jsonParse(@message) as span
        | unnest span.attributes.`app.question_flags` into rating
        | stats count(*) as questions by rating
        | sort questions desc
      EOT
    },
  ]
  # Two panels to a row below the Lambda panels, except the three on sources and
  # coverage, which share one.
  panel_place = [
    [12, 6, 12], [0, 12, 12], [12, 12, 6], [18, 12, 6], [0, 18, 12], [12, 18, 12],
    [0, 24, 12], [12, 24, 12], [0, 30, 8], [8, 30, 8], [16, 30, 8], [0, 36, 12], [12, 36, 12],
  ]
}

resource "aws_cloudwatch_dashboard" "main" {
  count          = local.deployed ? 1 : 0
  dashboard_name = local.name

  dashboard_body = jsonencode({
    start = "-PT168H"
    widgets = concat([
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "App requests, errors and throttles"
          region = var.aws_region
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", local.app_fn],
            ["AWS/Lambda", "Errors", "FunctionName", local.app_fn],
            ["AWS/Lambda", "Throttles", "FunctionName", local.app_fn],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "App duration, p50 and p95 (ms)"
          region = var.aws_region
          period = 300
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", local.app_fn, { stat = "p50" }],
            ["AWS/Lambda", "Duration", "FunctionName", local.app_fn, { stat = "p95" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Weekly ingest: runs, errors and duration (ms)"
          region = var.aws_region
          period = 86400
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", local.ingest_fn, { stat = "Sum" }],
            ["AWS/Lambda", "Errors", "FunctionName", local.ingest_fn, { stat = "Sum" }],
            ["AWS/Lambda", "Duration", "FunctionName", local.ingest_fn, { stat = "Maximum", yAxis = "right" }],
          ]
        }
      },
      ],
      [
        for i, panel in local.answer_panels : {
          type   = "log"
          x      = local.panel_place[i][0]
          y      = local.panel_place[i][1]
          width  = local.panel_place[i][2]
          height = 6
          properties = {
            title  = panel.title
            region = var.aws_region
            view   = panel.view
            query  = "SOURCE 'aws/spans' | ${trimspace(panel.query)}"
          }
        }
      ],
      [
        {
          type   = "alarm"
          x      = 0
          y      = 42
          width  = 12
          height = 4
          properties = {
            title = "Alarms"
            alarms = [
              aws_cloudwatch_metric_alarm.errors[0].arn,
              aws_cloudwatch_metric_alarm.throttles[0].arn,
              aws_cloudwatch_metric_alarm.slow[0].arn,
              aws_cloudwatch_metric_alarm.ingest_failed[0].arn,
              aws_cloudwatch_metric_alarm.mostly_declined[0].arn,
              aws_cloudwatch_metric_alarm.failovers[0].arn,
              aws_cloudwatch_metric_alarm.blocked_answer[0].arn,
            ]
          }
        },
        {
          type   = "text"
          x      = 12
          y      = 42
          width  = 12
          height = 4
          properties = {
            markdown = "**One answer's steps:** CloudWatch → Application Signals → Transaction Search, and search for the trace ID the page shows under the answer. The panels above count spans, which hold no question or answer text. Costs are list prices, not the bill."
          }
        },
    ])
  })
}

resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

resource "azurerm_consumption_budget_resource_group" "monthly" {
  name              = "budget-${local.name}-monthly"
  resource_group_id = azurerm_resource_group.main.id
  amount            = var.azure_monthly_budget
  time_grain        = "Monthly"

  time_period {
    # Azure wants the first of a month. Set once at creation; ignored afterwards.
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())
  }

  notification {
    enabled        = true
    threshold      = 80
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = [var.alert_email]
  }

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThan"
    threshold_type = "Forecasted"
    contact_emails = [var.alert_email]
  }

  lifecycle {
    ignore_changes = [time_period]
  }
}
