# ---------------------------------------------------------------------------
# Service Account — Cloud Run runtime
# ---------------------------------------------------------------------------
resource "google_service_account" "sofia_run" {
  account_id   = "sofia-cloudrun-sa"
  display_name = "Sofia Multi-Agent Cloud Run SA"
  description  = "SA usato dai container Cloud Run del progetto Sofia"
}

# ---------------------------------------------------------------------------
# IAM — Secret Manager: lettura secrets
# Chiavi statiche per evitare "unknown value in for_each" al plan time
# ---------------------------------------------------------------------------
resource "google_secret_manager_secret_iam_member" "sofia_run_secrets" {
  for_each = {
    auth_pg_dsn    = google_secret_manager_secret.auth_pg_dsn.secret_id
    redis_url      = google_secret_manager_secret.redis_url.secret_id
    openai_api_key = google_secret_manager_secret.openai_api_key.secret_id
    google_api_key = google_secret_manager_secret.google_api_key.secret_id
  }

  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.sofia_run.email}"
}

# ---------------------------------------------------------------------------
# IAM — Artifact Registry: lettura immagini
# ---------------------------------------------------------------------------
resource "google_artifact_registry_repository_iam_member" "sofia_run_read" {
  repository = google_artifact_registry_repository.sofia.name
  location   = var.region
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.sofia_run.email}"
}

# ---------------------------------------------------------------------------
# IAM — Cloud SQL client
# ---------------------------------------------------------------------------
resource "google_project_iam_member" "sofia_run_sql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.sofia_run.email}"
}

# ---------------------------------------------------------------------------
# IAM — Cloud Run invoker (accesso pubblico opzionale)
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  for_each = var.cloud_run_invoker_public ? local.agents : {}

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.sofia[each.key].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
output "service_account_email" {
  description = "Email Service Account Cloud Run"
  value       = google_service_account.sofia_run.email
}