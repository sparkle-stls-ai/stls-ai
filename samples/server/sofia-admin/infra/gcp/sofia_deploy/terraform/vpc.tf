# ---------------------------------------------------------------------------
# VPC Network
# ---------------------------------------------------------------------------
resource "google_compute_network" "sofia" {
  name                    = var.vpc_name
  auto_create_subnetworks = false

  depends_on = [google_project_service.apis]
}

resource "google_compute_subnetwork" "sofia" {
  name          = "${var.vpc_name}-subnet"
  ip_cidr_range = var.subnet_cidr
  region        = var.region
  network       = google_compute_network.sofia.id

  private_ip_google_access = true
}

# ---------------------------------------------------------------------------
# Private Services Access
# Richiesto da: Cloud SQL (private IP) e Memorystore PRIVATE_SERVICE_ACCESS
# ---------------------------------------------------------------------------
resource "google_compute_global_address" "private_ip_range" {
  name          = "${var.vpc_name}-private-ip-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.private_ip_prefix_length
  network       = google_compute_network.sofia.id
}

resource "google_service_networking_connection" "private_vpc" {
  network                 = google_compute_network.sofia.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_ip_range.name]

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Serverless VPC Connector
# Permette a Cloud Run di raggiungere IP privati (Redis, Cloud SQL)
# ---------------------------------------------------------------------------
resource "google_vpc_access_connector" "sofia" {
  name          = "sofia-connector"
  region        = var.region
  ip_cidr_range = var.connector_cidr
  network       = google_compute_network.sofia.name

  min_instances = 2
  max_instances = 3
  machine_type  = "e2-micro"

  depends_on = [
    google_project_service.apis,
    google_compute_network.sofia,
  ]
}

# ---------------------------------------------------------------------------
# Firewall — consente traffico interno VPC (agenti → Redis, Cloud SQL)
# ---------------------------------------------------------------------------
resource "google_compute_firewall" "allow_internal" {
  name    = "${var.vpc_name}-allow-internal"
  network = google_compute_network.sofia.name

  allow {
    protocol = "tcp"
    ports    = ["5432", "6379", "8080", "8081"]
  }

  source_ranges = [
    var.subnet_cidr,
    var.connector_cidr,
    "10.0.0.0/8", # copertura PSA
  ]

  description = "Traffico interno: Cloud Run → Cloud SQL e Memorystore"
}