terraform {
  required_version = ">= 1.6"

  required_providers {
    # 6.37 or later: data.tf uses the key_schema syntax for its index, and
    # 6.37 fixed a bug where removing such an index deleted all of them.
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.37"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State is local by default. It contains API keys, so before sharing it,
  # move it to an encrypted S3 bucket you create once by hand:
  #
  # backend "s3" {
  #   bucket       = "policy-desk-tfstate-<account-id>"
  #   key          = "policy-desk/terraform.tfstate"
  #   region       = "ap-southeast-2"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.azure_subscription_id
}

provider "azuread" {
  tenant_id = var.entra_tenant_id
}
