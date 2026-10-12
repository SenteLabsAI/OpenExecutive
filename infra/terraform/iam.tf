# Distinct, least-privilege service account for colette-exec — not the
# family product's colette-app SA. Secret access is scoped to exactly the
# secrets this app needs; no Cloud SQL role (SQLite-on-volume, see
# infra/README.md for why we didn't provision a Cloud SQL database).

resource "google_service_account" "app" {
  project      = var.project_id
  account_id   = "colette-exec-app"
  display_name = "Colette Exec app (CFO/GC/COO advisor)"
}

resource "google_secret_manager_secret_iam_member" "app_secret_access" {
  for_each = {
    backend_shared_secret   = google_secret_manager_secret.backend_shared_secret.secret_id
    auth_secret             = google_secret_manager_secret.auth_secret.secret_id
    anthropic_api_key       = google_secret_manager_secret.anthropic_api_key.secret_id
    google_oauth_client_id  = google_secret_manager_secret.google_oauth_client_id.secret_id
    google_oauth_client_secret = google_secret_manager_secret.google_oauth_client_secret.secret_id
    linear_api_key             = google_secret_manager_secret.linear_api_key.secret_id
    stripe_readonly_key        = google_secret_manager_secret.stripe_readonly_key.secret_id
  }

  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}
