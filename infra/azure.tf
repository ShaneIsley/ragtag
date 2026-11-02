resource "azurerm_resource_group" "main" {
  name     = "rg-${local.name}"
  location = var.azure_location
  tags     = local.tags
}

# ---------------------------------------------------------------- Azure OpenAI

resource "azurerm_cognitive_account" "openai" {
  name                          = "oai-${local.name}-${random_string.suffix.result}"
  custom_subdomain_name         = "oai-${local.name}-${random_string.suffix.result}"
  location                      = azurerm_resource_group.main.location
  resource_group_name           = azurerm_resource_group.main.name
  kind                          = "OpenAI"
  sku_name                      = "S0"
  local_auth_enabled            = true
  public_network_access_enabled = true
  tags                          = local.tags
}

resource "azurerm_cognitive_deployment" "embedding" {
  name                 = var.embedding_model_name
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = var.embedding_model_name
    version = var.embedding_model_version
  }

  sku {
    name     = var.embedding_deployment_type
    capacity = var.embedding_capacity
  }
}

resource "azurerm_cognitive_deployment" "chat" {
  name                 = var.chat_model_name
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = var.chat_model_name
    version = var.chat_model_version
  }

  sku {
    name     = var.chat_deployment_type
    capacity = var.chat_capacity
  }

  # Azure's default upgrades a deployment when a new default version is released,
  # changing the model under the service unannounced. This waits for the current
  # version to retire; the scheduled evaluation records the version and marks a
  # change (decisions, "Model version upgrades").
  version_upgrade_option = "OnceCurrentVersionExpired"

  # Azure rejects concurrent deployments on one account.
  depends_on = [azurerm_cognitive_deployment.embedding]
}

# ---------------------------------------------------------------- Azure AI Search
# Free tier: 50 MB, 3 indexes, one per subscription, no SLA, and it may be
# deleted after long inactivity. The ingest job creates the index from
# search/index.json if it doesn't exist, so a rebuild is one ingest run.

resource "azurerm_search_service" "search" {
  name                         = "srch-${local.name}-${random_string.suffix.result}"
  resource_group_name          = azurerm_resource_group.main.name
  location                     = azurerm_resource_group.main.location
  sku                          = "free"
  local_authentication_enabled = true
  tags                         = local.tags
}

# ---------------------------------------------------------------- Entra ID
# The admin view (flagged answers, traces) signs in with Entra ID. Only users
# assigned the PolicyDesk.Admin role can sign in to this app at all.

data "azuread_client_config" "current" {}

locals {
  admin_user_object_ids = var.admin_user_object_ids != null ? var.admin_user_object_ids : [data.azuread_client_config.current.object_id]
}

resource "random_uuid" "admin_role" {}

resource "azuread_application" "admin" {
  display_name     = "Policy Desk admin"
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azuread_client_config.current.object_id]

  app_role {
    allowed_member_types = ["User"]
    description          = "Review flagged answers and traces"
    display_name         = "Policy Desk admin"
    enabled              = true
    id                   = random_uuid.admin_role.result
    value                = "PolicyDesk.Admin"
  }

  web {
    implicit_grant {
      access_token_issuance_enabled = false
      id_token_issuance_enabled     = false
    }
  }

  # Redirect URIs are managed by azuread_application_redirect_uris below.
  lifecycle {
    ignore_changes = [web[0].redirect_uris]
  }
}

# Separate from the application: the app function needs the application's client
# ID, and CloudFront needs the function, so the application can't also depend
# on CloudFront.
resource "azuread_application_redirect_uris" "admin" {
  application_id = azuread_application.admin.id
  type           = "Web"
  redirect_uris = concat(
    try(["https://${aws_cloudfront_distribution.app[0].domain_name}/admin/callback"], []),
    ["http://localhost:8001/admin/callback"],
  )
}

resource "azuread_service_principal" "admin" {
  client_id                    = azuread_application.admin.client_id
  app_role_assignment_required = true
  owners                       = [data.azuread_client_config.current.object_id]
}

resource "azuread_app_role_assignment" "admins" {
  for_each            = toset(local.admin_user_object_ids)
  app_role_id         = azuread_service_principal.admin.app_role_ids["PolicyDesk.Admin"]
  principal_object_id = each.value
  resource_object_id  = azuread_service_principal.admin.object_id
}

resource "azuread_application_password" "admin" {
  application_id = azuread_application.admin.id
  display_name   = "policy-desk-app"
  end_date       = var.entra_secret_end_date
}
