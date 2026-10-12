# Secret containers for colette-exec, distinct from the family product's
# secrets (colette-db-password, anthropic-api-key, etc. in chatelaine's own
# terraform) even though both live in the same GCP project.
#
# BACKEND_SHARED_SECRET and AUTH_SECRET are internal tokens (not third-party
# credentials) — safe for Terraform to generate and populate directly.
# ANTHROPIC_API_KEY and the Google OAuth client id/secret are populated
# out-of-band (see infra/README.md) so they never pass through a .tf file,
# tfvars, or state diff.

resource "random_password" "backend_shared_secret" {
  length  = 64
  special = false
}

resource "random_password" "auth_secret" {
  length  = 64
  special = false
}

resource "google_secret_manager_secret" "backend_shared_secret" {
  secret_id = "colette-exec-backend-shared-secret"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "backend_shared_secret" {
  secret      = google_secret_manager_secret.backend_shared_secret.id
  secret_data = random_password.backend_shared_secret.result
}

resource "google_secret_manager_secret" "auth_secret" {
  secret_id = "colette-exec-auth-secret"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "auth_secret" {
  secret      = google_secret_manager_secret.auth_secret.id
  secret_data = random_password.auth_secret.result
}

resource "google_secret_manager_secret" "anthropic_api_key" {
  secret_id = "colette-exec-anthropic-api-key"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "google_oauth_client_id" {
  secret_id = "colette-exec-google-client-id"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "google_oauth_client_secret" {
  secret_id = "colette-exec-google-client-secret"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

# Operating-data feeds for the CFO/COO/partner seats (read-only keys).
# Containers only: West populates the values himself (see docs/deployment.md).
# A secret with no version is skipped by deploy.sh and the feed reports
# "not configured" instead of failing.
resource "google_secret_manager_secret" "linear_api_key" {
  secret_id = "colette-exec-linear-api-key"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "stripe_readonly_key" {
  secret_id = "colette-exec-stripe-readonly-key"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}
