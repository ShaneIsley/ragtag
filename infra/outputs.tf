output "app_url" {
  description = "The app's public HTTPS address."
  value       = try("https://${aws_cloudfront_distribution.app[0].domain_name}", "Not deployed: push the images and set image_tag")
}

output "local_env" {
  description = "Settings for local development. Write them with: terraform output -raw local_env > ../.env"
  sensitive   = true
  value       = <<-EOT
    AZURE_OPENAI_ENDPOINT=${azurerm_cognitive_account.openai.endpoint}
    AZURE_OPENAI_API_KEY=${azurerm_cognitive_account.openai.primary_access_key}
    AZURE_OPENAI_CHAT_DEPLOYMENT=${azurerm_cognitive_deployment.chat.name}
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT=${azurerm_cognitive_deployment.embedding.name}
    SEARCH_ENDPOINT=https://${azurerm_search_service.search.name}.search.windows.net
    SEARCH_INDEX=${local.search_index_name}
    SEARCH_QUERY_KEY=${azurerm_search_service.search.query_keys[0].key}
    SEARCH_ADMIN_KEY=${azurerm_search_service.search.primary_key}
    BEDROCK_REGION=${var.aws_region}
    BEDROCK_MODEL_ID=${var.bedrock_inference_profile_id}
    FAILOVER_DRILL=false
    FEEDBACK_TABLE=${aws_dynamodb_table.feedback.name}
    FEEDBACK_RETENTION_DAYS=${var.feedback_retention_days}
    ENTRA_TENANT_ID=${data.azuread_client_config.current.tenant_id}
    ENTRA_CLIENT_ID=${azuread_application.admin.client_id}
    ENTRA_CLIENT_SECRET=${azuread_application_password.admin.value}
    ENTRA_ADMIN_ROLE=PolicyDesk.Admin
    PUBLIC_BASE_URL=http://localhost:8001
    SESSION_SECRET=${random_password.session.result}
    APP_VERSION=
    EVAL_RECORDS_BUCKET=
    STATUS_SNAPSHOT_FILE=evals/results/status/snapshot.json
    OTEL_TRACES_EXPORTER=none
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
    OTEL_SERVICE_NAME=${local.name}-app-local
  EOT
}

output "ecr_app_repository" {
  description = "Push the app image here, tagged with the commit SHA."
  value       = aws_ecr_repository.this["app"].repository_url
}

output "ecr_ingest_repository" {
  description = "Push the ingest image here, tagged with the same SHA."
  value       = aws_ecr_repository.this["ingest"].repository_url
}

output "eval_function" {
  description = "The scheduled evaluation. Run a suite in the background (infra/README.md, Scheduled evaluation): aws lambda invoke --function-name <this> --invocation-type Event --payload '{\"suite\":\"canary\"}' --cli-binary-format raw-in-base64-out /dev/null"
  value       = try(aws_lambda_function.eval[0].function_name, "Not deployed")
}

output "eval_records_bucket" {
  description = "Where every evaluation run's record and results are kept"
  value       = aws_s3_bucket.eval_records.bucket
}

output "ecr_eval_repository" {
  value = aws_ecr_repository.this["eval"].repository_url
}

output "ingest_function" {
  description = "Run the ingest job now: aws lambda invoke --function-name <this> --invocation-type Event /dev/null"
  value       = try(aws_lambda_function.ingest[0].function_name, "Not deployed: push the images and set image_tag")
}

output "ci_env" {
  description = "The CI evaluation gate's settings: no secrets, since the CI role reads the keys. Set them as the GitHub variable APP_ENV (infra/README.md, CI and deployment)."
  value       = <<-EOT
    AZURE_OPENAI_ENDPOINT=${azurerm_cognitive_account.openai.endpoint}
    AZURE_OPENAI_CHAT_DEPLOYMENT=${azurerm_cognitive_deployment.chat.name}
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT=${azurerm_cognitive_deployment.embedding.name}
    AZURE_OPENAI_API_KEY_SECRET_ARN=${aws_secretsmanager_secret.this["azure-openai-key"].arn}
    SEARCH_ENDPOINT=https://${azurerm_search_service.search.name}.search.windows.net
    SEARCH_INDEX=${local.search_index_name}
    SEARCH_QUERY_KEY_SECRET_ARN=${aws_secretsmanager_secret.this["search-query-key"].arn}
    BEDROCK_REGION=${var.aws_region}
    BEDROCK_MODEL_ID=${var.bedrock_inference_profile_id}
    EVAL_RECORDS_BUCKET=${aws_s3_bucket.eval_records.bucket}
    OTEL_TRACES_EXPORTER=none
  EOT
}

output "github_ci_role_arn" {
  description = "Set as AWS_CI_ROLE_ARN in the repository's Actions variables."
  value       = aws_iam_role.github_ci.arn
}

output "github_deploy_role_arn" {
  description = "Set as AWS_DEPLOY_ROLE_ARN in the repository's Actions variables."
  value       = aws_iam_role.github_deploy.arn
}

output "azure_openai_endpoint" {
  value = azurerm_cognitive_account.openai.endpoint
}

output "search_endpoint" {
  value = "https://${azurerm_search_service.search.name}.search.windows.net"
}

output "entra_admin_client_id" {
  value = azuread_application.admin.client_id
}

output "dashboard_url" {
  value = try("https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards/dashboard/${aws_cloudwatch_dashboard.main[0].dashboard_name}", "Not deployed: push the images and set image_tag")
}
