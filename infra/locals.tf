data "aws_caller_identity" "current" {}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

locals {
  name = var.project

  # The functions, CloudFront, alarms and the schedule need a pushed image.
  deployed = var.image_tag != ""

  search_index_name = "policy-desk-chunks"

  # Classification and risk rating follow UNSW's Cyber Security Policy (asset
  # inventory, §1.1): the corpus is Public, and with no personal data stored
  # unmasked the service would rate Low.
  tags = {
    project             = var.project
    managed_by          = "terraform"
    repository          = var.github_repository
    data_classification = "Public"
    risk_rating         = "Low"
  }

  bedrock_profile_arn = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_inference_profile_id}"
  bedrock_model_arns  = [for r in var.bedrock_destination_regions : "arn:aws:bedrock:${r}::foundation-model/${var.bedrock_foundation_model_id}"]
}
