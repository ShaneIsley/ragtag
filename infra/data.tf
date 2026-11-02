# ---------------------------------------------------------------- flagged answers

resource "aws_dynamodb_table" "feedback" {
  name         = "${local.name}-feedback"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  # The admin review queue: newest flagged answers by status.
  global_secondary_index {
    name            = "by_status"
    projection_type = "ALL"

    key_schema {
      attribute_name = "status"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "created_at"
      key_type       = "RANGE"
    }
  }

  # Items carry expires_at; DynamoDB deletes them after the retention period.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}

# ---------------------------------------------------------------- secrets

resource "random_password" "session" {
  length  = 48
  special = false
}

locals {
  secret_names = toset([
    "azure-openai-key",
    "search-query-key",
    "search-admin-key",
    "entra-client-secret",
    "session-secret",
  ])

  secret_values = {
    "azure-openai-key"    = azurerm_cognitive_account.openai.primary_access_key
    "search-query-key"    = azurerm_search_service.search.query_keys[0].key
    "search-admin-key"    = azurerm_search_service.search.primary_key
    "entra-client-secret" = azuread_application_password.admin.value
    "session-secret"      = random_password.session.result
  }
}

resource "aws_secretsmanager_secret" "this" {
  for_each                = local.secret_names
  name                    = "${local.name}/${each.key}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "this" {
  for_each      = local.secret_names
  secret_id     = aws_secretsmanager_secret.this[each.key].id
  secret_string = local.secret_values[each.key]
}
