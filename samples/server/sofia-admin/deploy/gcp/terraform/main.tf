terraform {
  required_version = ">= 1.7"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.0"
    }
  }

  # ── Backend GCS (decommenta dopo aver creato il bucket) ──────────────────
  backend "gcs" {
  bucket = "aichemy-dev-01-tf-state"
  prefix = "terraform/sofia"
   }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# ---------------------------------------------------------------------------
# API Google abilitate
# ---------------------------------------------------------------------------
locals {
  required_apis = [
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "sql-component.googleapis.com",
    "redis.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "vpcaccess.googleapis.com",
    "servicenetworking.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)

  project                    = var.project_id
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false
}

# ---------------------------------------------------------------------------
# Locals condivisi
# ---------------------------------------------------------------------------
locals {
  # Mappa dei tre agenti: chiave = suffisso usato ovunque
  agents = {
    api-keys = {
      description = "Sofia API Keys Management Agent"
      path        = "/agents/sofia-api-keys/secure-a2a"
    }
    monitoring = {
      description = "Sofia Monitoring Agent"
      path        = "/agents/sofia-monitoring/secure-a2a"
    }
    redis = {
      description = "Sofia Redis Agent"
      path        = "/agents/sofia-redis/secure-a2a"
    }
  }

  # Tag immagine per ciascun agente (override via tfvars)
  image_tags = {
    api-keys   = lookup(var.image_tags, "api-keys", var.default_image_tag)
    monitoring = lookup(var.image_tags, "monitoring", var.default_image_tag)
    redis      = lookup(var.image_tags, "redis", var.default_image_tag)
  }

  # Registry base path
  registry_base = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.sofia.repository_id}"
}