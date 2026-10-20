# ---------------------------------------------------------------------------
# Outputs — riepilogo infrastruttura deployata
# ---------------------------------------------------------------------------

output "summary" {
  description = "Riepilogo infrastruttura Sofia"
  value = {
    project    = var.project_id
    region     = var.region
    registry   = "${var.region}-docker.pkg.dev/${var.project_id}/sofia"
    db = {
      instance   = google_sql_database_instance.sofia.name
      private_ip = google_sql_database_instance.sofia.private_ip_address
      connection = google_sql_database_instance.sofia.connection_name
      database   = var.db_name
      user       = var.db_user
    }
    redis = {
      host = google_redis_instance.sofia.host
      port = google_redis_instance.sofia.port
    }
    vpc = {
      network   = google_compute_network.sofia.name
      subnet    = google_compute_subnetwork.sofia.name
      connector = google_vpc_access_connector.sofia.name
    }
    services = {
      for k, svc in google_cloud_run_v2_service.sofia : k => {
        url  = svc.uri
        name = svc.name
      }
    }
  }
}