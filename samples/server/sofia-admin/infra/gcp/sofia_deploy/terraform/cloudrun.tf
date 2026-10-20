# ---------------------------------------------------------------------------
# Cloud Run v2 — tre servizi agente via for_each
#
# BASE_URL — deploy in due fasi:
#   Fase 1: terraform apply  (base_urls vuoto → localhost, solo per creare i servizi)
#   Fase 2: leggi service_urls dall'output, inseriscili in base_urls nel tfvars
#           terraform apply  (BASE_URL corretti → agent card corretta)
# ---------------------------------------------------------------------------

variable "base_urls" {
  description = "BASE_URL per ciascun agente. Chiavi: api-keys | monitoring | redis. Compilare dopo la Fase 1."
  type        = map(string)
  default     = {}
}

resource "google_cloud_run_v2_service" "sofia" {
  for_each = local.agents

  name     = "sofia-${each.key}"
  location = var.region

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  template {
    service_account = google_service_account.sofia_run.email

    vpc_access {
      connector = google_vpc_access_connector.sofia.id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = "${local.registry_base}/server-${each.key}:${local.image_tags[each.key]}"

      ports {
        name           = "http1"
        container_port = 8080
      }

      env {
        name  = "BASE_URL"
        value = lookup(var.base_urls, each.key, "")
      }

      env {
        name  = "TASK_STORE_BACKEND"
        value = "redis"
      }

      env {
        name  = "REDIS_KEY_PREFIX"
        value = "stls"
      }

      env {
        name  = "TASK_STORE_TTL_SECONDS"
        value = tostring(var.task_store_ttl_seconds)
      }

      env {
        name  = "TASK_STORE_CLEANUP_ENABLED"
        value = "true"
      }

      env {
        name  = "TASK_STORE_CLEANUP_INTERVAL"
        value = tostring(var.task_store_cleanup_interval)
      }

      env {
        name  = "AUTH_LOADER_BACKEND"
        value = "postgres"
      }

      env {
        name  = "SF_API_KEYS_TABLE"
        value = "sf_api_keys"
      }

      env {
        name  = "SF_AGENTS_TABLE"
        value = "sf_agents"
      }

      env {
        name  = "SF_HEARTBEAT_INTERVAL"
        value = tostring(var.sf_heartbeat_interval)
      }

      env {
        name  = "SF_LIVENESS_THRESHOLD"
        value = tostring(var.sf_liveness_threshold)
      }

      env {
        name  = "AUTH_KM_USE_REDIS"
        value = "true"
      }

      env {
        name  = "AUTH_KEY_TTL"
        value = tostring(var.auth_key_ttl)
      }

      env {
        name  = "AUTH_REFRESH_DISTRIBUTED"
        value = "true"
      }

      env {
        name  = "AUTH_REFRESH_INTERVAL"
        value = tostring(var.auth_refresh_interval)
      }

      env {
        name  = "KEY_SESSION_PORT"
        value = "8081"
      }

      env {
        name  = "KEY_PROVIDER_NAME"
        value = "base"
      }

      env {
        name = "AUTH_PG_DSN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.auth_pg_dsn.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "REDIS_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.redis_url.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.openai_api_key.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "GOOGLE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.google_api_key.secret_id
            version = "latest"
          }
        }
      }

      resources {
        limits = {
          cpu    = var.cloud_run_cpu
          memory = var.cloud_run_memory
        }
        cpu_idle = true
      }

      liveness_probe {
        http_get {
          path = "/auth/health"
          port = 8080
        }
        initial_delay_seconds = 15
        period_seconds        = 30
        failure_threshold     = 3
      }

      startup_probe {
        http_get {
          path = "/auth/health"
          port = 8080
        }
        initial_delay_seconds = 10
        period_seconds        = 15
        failure_threshold     = 10
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.sofia_run_secrets,
    google_vpc_access_connector.sofia,
  ]
}

output "service_urls" {
  description = "URL pubblici Cloud Run — copiare in base_urls nel tfvars per la Fase 2"
  value = {
    for k, svc in google_cloud_run_v2_service.sofia : k => svc.uri
  }
}