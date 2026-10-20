# ---------------------------------------------------------------------------
# Secret Manager — variabili sensibili
# Strategia: Terraform crea i secret e ne carica il valore iniziale.
# Le rotazioni successive avvengono fuori da Terraform (gcloud / CI/CD).
# ---------------------------------------------------------------------------

# Helper locale: costruisce i DSN dopo che gli IP sono noti
locals {
  pg_dsn    = "postgresql://${var.db_user}:${var.db_password}@${google_sql_database_instance.sofia.private_ip_address}/${var.db_name}"
  redis_url = "redis://${google_redis_instance.sofia.host}:${google_redis_instance.sofia.port}"
}

# ---------------------------------------------------------------------------
# AUTH_PG_DSN
# ---------------------------------------------------------------------------
resource "google_secret_manager_secret" "auth_pg_dsn" {
  secret_id = "sofia-auth-pg-dsn"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "auth_pg_dsn" {
  secret      = google_secret_manager_secret.auth_pg_dsn.id
  secret_data = local.pg_dsn

  lifecycle {
    # Non sovrascrivere il secret se è stato aggiornato manualmente
    ignore_changes = [secret_data]
  }
}

# ---------------------------------------------------------------------------
# REDIS_URL
# ---------------------------------------------------------------------------
resource "google_secret_manager_secret" "redis_url" {
  secret_id = "sofia-redis-url"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "redis_url" {
  secret      = google_secret_manager_secret.redis_url.id
  secret_data = local.redis_url

  lifecycle {
    ignore_changes = [secret_data]
  }
}

# ---------------------------------------------------------------------------
# OPENAI_API_KEY
# ---------------------------------------------------------------------------
resource "google_secret_manager_secret" "openai_api_key" {
  secret_id = "sofia-openai-api-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "openai_api_key" {
  secret      = google_secret_manager_secret.openai_api_key.id
  secret_data = var.openai_api_key

  lifecycle {
    ignore_changes = [secret_data]
  }
}

# ---------------------------------------------------------------------------
# GOOGLE_API_KEY
# ---------------------------------------------------------------------------
resource "google_secret_manager_secret" "google_api_key" {
  secret_id = "sofia-google-api-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "google_api_key" {
  secret      = google_secret_manager_secret.google_api_key.id
  secret_data = var.google_api_key

  lifecycle {
    ignore_changes = [secret_data]
  }
}

# ---------------------------------------------------------------------------
# Output secret IDs (utili per referenziare in Cloud Run)
# ---------------------------------------------------------------------------
output "secret_ids" {
  description = "Secret Manager secret IDs"
  value = {
    auth_pg_dsn    = google_secret_manager_secret.auth_pg_dsn.secret_id
    redis_url      = google_secret_manager_secret.redis_url.secret_id
    openai_api_key = google_secret_manager_secret.openai_api_key.secret_id
    google_api_key = google_secret_manager_secret.google_api_key.secret_id
  }
}