# ---------------------------------------------------------------------------
# Progetto GCP
# ---------------------------------------------------------------------------
variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP region per tutti i servizi"
  type        = string
  default     = "europe-west8"
}

# ---------------------------------------------------------------------------
# Immagini Docker
# ---------------------------------------------------------------------------
variable "default_image_tag" {
  description = "Tag Docker di default usato per tutti i servizi se non sovrascritto"
  type        = string
  default     = "latest"
}

variable "image_tags" {
  description = "Tag Docker per ciascun agente. Chiavi: api-keys | monitoring | redis"
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------
# Cloud SQL (PostgreSQL)
# ---------------------------------------------------------------------------
variable "db_instance_name" {
  description = "Nome istanza Cloud SQL"
  type        = string
  default     = "sofia-pg"
}

variable "db_tier" {
  description = "Tier Cloud SQL. Usa db-f1-micro per dev, db-g1-small/db-n1-standard-2 per prod"
  type        = string
  default     = "db-g1-small"
}

variable "db_availability_type" {
  description = "ZONAL (dev) o REGIONAL (prod HA)"
  type        = string
  default     = "ZONAL"
  validation {
    condition     = contains(["ZONAL", "REGIONAL"], var.db_availability_type)
    error_message = "Deve essere ZONAL o REGIONAL."
  }
}

variable "db_name" {
  description = "Nome database PostgreSQL"
  type        = string
  default     = "sofia"
}

variable "db_user" {
  description = "Utente database PostgreSQL"
  type        = string
  default     = "spk_sofia"
}

variable "db_password" {
  description = "Password database PostgreSQL (sensibile)"
  type        = string
  sensitive   = true
}

variable "db_deletion_protection" {
  description = "Protegge il db da cancellazione accidentale via Terraform"
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Redis (Memorystore)
# ---------------------------------------------------------------------------
variable "redis_instance_name" {
  description = "Nome istanza Memorystore Redis"
  type        = string
  default     = "sofia-redis"
}

variable "redis_memory_gb" {
  description = "Dimensione memoria Redis in GB"
  type        = number
  default     = 1
}

variable "redis_tier" {
  description = "BASIC (nessuna HA) o STANDARD_HA (replica)"
  type        = string
  default     = "BASIC"
  validation {
    condition     = contains(["BASIC", "STANDARD_HA"], var.redis_tier)
    error_message = "Deve essere BASIC o STANDARD_HA."
  }
}

variable "redis_version" {
  description = "Versione Redis"
  type        = string
  default     = "REDIS_7_0"
}

# ---------------------------------------------------------------------------
# Rete VPC
# ---------------------------------------------------------------------------
variable "vpc_name" {
  description = "Nome VPC network"
  type        = string
  default     = "sofia-network"
}

variable "subnet_cidr" {
  description = "CIDR del subnet principale"
  type        = string
  default     = "10.8.0.0/24"
}

variable "connector_cidr" {
  description = "CIDR /28 per il VPC Connector (serverless → VPC). Deve essere non sovrapposto."
  type        = string
  default     = "10.8.1.0/28"
}

variable "private_ip_prefix_length" {
  description = "Prefisso /N per private services access (Cloud SQL, Redis PSA)"
  type        = number
  default     = 16
}

# ---------------------------------------------------------------------------
# Cloud Run
# ---------------------------------------------------------------------------
variable "cloud_run_cpu" {
  description = "CPU allocata per container Cloud Run"
  type        = string
  default     = "1"
}

variable "cloud_run_memory" {
  description = "Memoria per container Cloud Run"
  type        = string
  default     = "512Mi"
}

variable "min_instances" {
  description = "Numero minimo di istanze per servizio Cloud Run (0 = scale-to-zero)"
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Numero massimo di istanze per servizio Cloud Run"
  type        = number
  default     = 5
}

variable "cloud_run_invoker_public" {
  description = "Se true, aggiunge IAM binding allUsers→roles/run.invoker (accesso pubblico)"
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Secrets (valori sensibili da passare a Terraform)
# ---------------------------------------------------------------------------
variable "openai_api_key" {
  description = "OpenAI API key"
  type        = string
  sensitive   = true
}

variable "google_api_key" {
  description = "Google API key (Gemini / Google AI)"
  type        = string
  sensitive   = true
}

# ---------------------------------------------------------------------------
# Configurazione applicativa (non sensibile)
# ---------------------------------------------------------------------------
variable "task_store_ttl_seconds" {
  type    = number
  default = 360
}

variable "task_store_cleanup_interval" {
  type    = number
  default = 180
}

variable "auth_key_ttl" {
  type    = number
  default = 3600
}

variable "auth_refresh_interval" {
  type    = number
  default = 300
}

variable "sf_heartbeat_interval" {
  type    = number
  default = 120
}

variable "sf_liveness_threshold" {
  type    = number
  default = 360
}