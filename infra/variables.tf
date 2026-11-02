# ---------------------------------------------------------------- general

variable "project" {
  description = "Short name used for every resource."
  type        = string
  default     = "policy-desk"
}

variable "image_tag" {
  description = "Container image tag (the commit SHA) for both functions. Leave empty until both images are pushed: the functions, CloudFront, alarms and the weekly schedule are created only when this is set."
  type        = string
  default     = ""
}

variable "app_memory_mb" {
  description = "Memory for the app function, in MB. Lambda gives CPU in proportion. Presidio's language model (Phase 2) needs about 1 GB."
  type        = number
  default     = 1024
}

variable "app_reserved_concurrency" {
  description = "Most requests the app handles at once, which caps what a flood of requests can cost. -1 means no reservation, leaving the account's quota as the only cap. Lambda keeps at least 100 executions unreserved (all of them on a new account's quota of 10), so a reservation needs a quota above 100."
  type        = number
  default     = -1
}

variable "ingest_memory_mb" {
  description = "Memory for the weekly ingest function, in MB."
  type        = number
  default     = 2048
}

variable "alert_email" {
  description = "Email address for alarms and budget alerts. Confirm the SNS subscription email after the first apply."
  type        = string
}

variable "github_repository" {
  description = "The GitHub repository that runs CI/CD, as owner/name. Tags every resource."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use the form owner/name, for example ShaneIsley/policy-desk."
  }
}

variable "github_oidc_repository" {
  description = <<-EOT
    The same repository as GitHub's OIDC tokens name it, which the CI and deploy roles
    trust: owner@owner_id/name@repository_id for newer repositories (2026), so a renamed
    or re-created repository can't take over the trust. A refused sign-in's userName in
    CloudTrail shows it. Empty means owner/name, the older form.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.github_oidc_repository == "" || can(regex("^[A-Za-z0-9_.-]+(@[0-9]+)?/[A-Za-z0-9_.-]+(@[0-9]+)?$", var.github_oidc_repository))
    error_message = "Use owner@owner_id/name@repository_id, for example ShaneIsley@7237/ragtag@1441741671."
  }
}

# ---------------------------------------------------------------- AWS

variable "aws_region" {
  description = "AWS region for hosting and Bedrock."
  type        = string
  default     = "ap-southeast-2"

  validation {
    condition     = var.aws_region == "ap-southeast-2"
    error_message = "Keep AWS in ap-southeast-2 (Sydney). See README, Data residency."
  }
}

variable "ingest_schedule" {
  description = "EventBridge Scheduler expression for the ingest job, in Australia/Sydney time."
  type        = string
  default     = "cron(0 3 ? * MON *)"
}

variable "log_retention_days" {
  description = "How long CloudWatch keeps application logs."
  type        = number
  default     = 30
}

variable "feedback_retention_days" {
  description = "How long flagged answers are kept before DynamoDB deletes them. The app sets expires_at from this."
  type        = number
  default     = 90
}

variable "latency_alarm_seconds" {
  description = "Alarm when p95 response time from the app stays above this."
  type        = number
  default     = 15
}

variable "monthly_budget_usd" {
  description = "Monthly AWS budget in USD. Alerts at 80% actual and 100% forecast. Expected spend is about US$2 a month (secrets), with Lambda and CloudFront inside their free tiers at demo traffic."
  type        = number
  default     = 5
}

variable "azure_monthly_budget" {
  description = "Monthly Azure budget for the resource group, in the subscription's billing currency (AUD for an Australian account). Alerts at 80% actual and 100% forecast. Expected spend is about A$3 a month with the scheduled evaluation."
  type        = number
  default     = 5
}

variable "bedrock_inference_profile_id" {
  description = "Bedrock inference profile for the failover model. The AU profile keeps processing in Sydney and Melbourne."
  type        = string
  default     = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "bedrock_foundation_model_id" {
  description = "Foundation model behind the inference profile."
  type        = string
  default     = "anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "bedrock_destination_regions" {
  description = "Regions the inference profile can route to."
  type        = list(string)
  default     = ["ap-southeast-2", "ap-southeast-4"]
}

# ---------------------------------------------------------------- Azure

variable "azure_subscription_id" {
  description = "Azure subscription for Azure OpenAI and Azure AI Search. Leave unset to use ARM_SUBSCRIPTION_ID; azurerm 4.x won't fall back to the Azure CLI's active subscription."
  type        = string
  default     = null
}

variable "azure_location" {
  description = "Azure region."
  type        = string
  default     = "australiaeast"

  validation {
    condition     = var.azure_location == "australiaeast"
    error_message = "Keep Azure in australiaeast. See README, Data residency."
  }
}

variable "chat_model_name" {
  description = "Azure OpenAI chat model."
  type        = string
  default     = "gpt-5-mini"
}

variable "chat_model_version" {
  description = "Azure OpenAI chat model version."
  type        = string
  default     = "2025-08-07"
}

variable "chat_deployment_type" {
  description = "Standard keeps processing in the region but few current models offer it in Australia East. GlobalStandard may process anywhere. See README, Data residency."
  type        = string
  default     = "GlobalStandard"

  validation {
    condition     = contains(["Standard", "GlobalStandard", "DataZoneStandard", "ProvisionedManaged", "GlobalProvisionedManaged"], var.chat_deployment_type)
    error_message = "Use an Azure OpenAI deployment type such as Standard or GlobalStandard."
  }
}

variable "chat_capacity" {
  description = "Chat deployment capacity. Each unit allows 1,000 tokens and, for gpt-5-mini, 1 request a minute. A question makes two requests of about 2,800 tokens in all, so 20 answers about 7 questions a minute and caps a flood at about US$14 a day."
  type        = number
  default     = 20
}

variable "embedding_model_name" {
  description = "Azure OpenAI embedding model. The search index expects 1,536 dimensions."
  type        = string
  default     = "text-embedding-3-small"
}

variable "embedding_model_version" {
  description = "Embedding model version."
  type        = string
  default     = "1"
}

variable "embedding_deployment_type" {
  description = "Standard keeps embedding in Australia East. If apply reports it unavailable, use GlobalStandard."
  type        = string
  default     = "Standard"
}

variable "embedding_capacity" {
  description = "Embedding deployment capacity, in thousands of tokens per minute."
  type        = number
  default     = 120
}

# ---------------------------------------------------------------- Entra ID

variable "entra_tenant_id" {
  description = "Entra ID tenant that holds the admin app registration. Leave unset to use the Azure CLI's signed-in tenant."
  type        = string
  default     = null
}

variable "admin_user_object_ids" {
  description = "Object IDs of the people who may open the admin view. Leave unset to grant it to whoever runs Terraform."
  type        = list(string)
  default     = null
}

variable "entra_secret_end_date" {
  description = "Expiry of the admin app's client secret (RFC 3339)."
  type        = string
  default     = "2027-06-30T00:00:00Z"
}
