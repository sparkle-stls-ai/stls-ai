# ---------------------------------------------------------------------------
# Artifact Registry — repository Docker per le immagini degli agenti
# ---------------------------------------------------------------------------
resource "google_artifact_registry_repository" "sofia" {
  repository_id = "sofia"
  format        = "DOCKER"
  location      = var.region
  description   = "Immagini Docker agenti Sofia Multi-Agent"

  cleanup_policies {
    id     = "keep-last-5"
    action = "KEEP"

    most_recent_versions {
      keep_count = 5
    }
  }

  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"

    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s"  # 7 giorni in secondi (168h non accettato dall'API)
    }
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
output "registry_url" {
  description = "URL base registry Docker"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.sofia.repository_id}"
}