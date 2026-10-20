# ---------------------------------------------------------------------------
# Cloud SQL — PostgreSQL 16
# ---------------------------------------------------------------------------
resource "google_sql_database_instance" "sofia" {
  name             = var.db_instance_name
  database_version = "POSTGRES_16"
  region           = var.region

  deletion_protection = var.db_deletion_protection

  settings {
    tier              = var.db_tier
    availability_type = var.db_availability_type
    disk_autoresize   = true
    disk_size         = 10  # GB iniziali

    # IP Configuration — SOLO private IP, nessun IP pubblico
    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = google_compute_network.sofia.id
      enable_private_path_for_google_cloud_services = true
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "03:00"
      transaction_log_retention_days = 7

      backup_retention_settings {
        retained_backups = 7
        retention_unit   = "COUNT"
      }
    }

    maintenance_window {
      day          = 7  # Domenica
      hour         = 4
      update_track = "stable"
    }

    insights_config {
      query_insights_enabled  = true
      query_string_length     = 1024
      record_application_tags = false
      record_client_address   = false
    }

    database_flags {
      name  = "max_connections"
      value = "100"
    }
  }

  depends_on = [google_service_networking_connection.private_vpc]
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
resource "google_sql_database" "sofia" {
  name     = var.db_name
  instance = google_sql_database_instance.sofia.name
}

# ---------------------------------------------------------------------------
# Utente DB
# ---------------------------------------------------------------------------
resource "google_sql_user" "sofia" {
  name     = var.db_user
  instance = google_sql_database_instance.sofia.name
  password = var.db_password

  # Impedisce la ricreazione dell'utente ad ogni apply
  lifecycle {
    ignore_changes = [password]
  }
}

# ---------------------------------------------------------------------------
# Output: private IP (usato per costruire AUTH_PG_DSN nel secret)
# ---------------------------------------------------------------------------
output "db_private_ip" {
  description = "IP privato Cloud SQL (usato in AUTH_PG_DSN)"
  value       = google_sql_database_instance.sofia.private_ip_address
  sensitive   = false
}

output "db_connection_name" {
  description = "Cloud SQL connection name (formato project:region:instance)"
  value       = google_sql_database_instance.sofia.connection_name
}