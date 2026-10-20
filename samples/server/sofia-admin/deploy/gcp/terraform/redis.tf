# ---------------------------------------------------------------------------
# Memorystore for Redis
# ---------------------------------------------------------------------------
resource "google_redis_instance" "sofia" {
  name           = var.redis_instance_name
  tier           = var.redis_tier
  memory_size_gb = var.redis_memory_gb
  region         = var.region
  redis_version  = var.redis_version

  # Rete privata — DIRECT_PEERING non richiede PSA aggiuntiva
  authorized_network = google_compute_network.sofia.id
  connect_mode       = "DIRECT_PEERING"

  # Configurazione Redis
  redis_configs = {
    maxmemory-policy = "allkeys-lru"
    notify-keyspace-events = ""  # disabilita keyspace events (risparmio CPU)
  }

  display_name = "Sofia Multi-Agent Redis"

  depends_on = [
    google_project_service.apis,
    google_compute_network.sofia,
  ]
}

# ---------------------------------------------------------------------------
# Output: host Redis (usato per costruire REDIS_URL nel secret)
# ---------------------------------------------------------------------------
output "redis_host" {
  description = "IP host Memorystore Redis"
  value       = google_redis_instance.sofia.host
}

output "redis_port" {
  description = "Porta Memorystore Redis"
  value       = google_redis_instance.sofia.port
}