#!/bin/bash
# Run this ON THE VM (as the eric_colette_family user, via
# `gcloud compute ssh colette-exec --tunnel-through-iap`) after populating
# the real secret values (see infra/README.md). Pulls this app's Secret
# Manager secrets into a root .env via the VM's attached colette-exec-app
# service account (no key file involved — metadata-server credentials),
# then brings the stack up.
set -euo pipefail
cd /opt/colette-exec

fetch() { gcloud secrets versions access latest --secret="$1"; }

{
  echo "ANTHROPIC_API_KEY=$(fetch colette-exec-anthropic-api-key)"
  echo "BACKEND_SHARED_SECRET=$(fetch colette-exec-backend-shared-secret)"
  echo "AUTH_SECRET=$(fetch colette-exec-auth-secret)"
  echo "AUTH_GOOGLE_ID=$(fetch colette-exec-google-client-id 2>/dev/null || true)"
  echo "AUTH_GOOGLE_SECRET=$(fetch colette-exec-google-client-secret 2>/dev/null || true)"
  echo "OE_PUBLIC_DEPLOYMENT=1"
  # Optional operating-data feeds: only written when a version exists, so an
  # unpopulated secret leaves the feed "not configured" rather than set-but-empty.
  for pair in "LINEAR_API_KEY:colette-exec-linear-api-key" "STRIPE_READONLY_KEY:colette-exec-stripe-readonly-key"; do
    if value="$(fetch "${pair#*:}" 2>/dev/null)" && [ -n "$value" ]; then
      echo "${pair%%:*}=$value"
    fi
  done
  # Not a secret: set in the VM environment or edit here, e.g. project.dataset.gcp_billing_export_v1_XXXX
  [ -n "${BILLING_EXPORT_TABLE:-}" ] && echo "BILLING_EXPORT_TABLE=$BILLING_EXPORT_TABLE"
} > .env
chmod 600 .env

sudo docker compose -f docker/docker-compose.yml --env-file .env up -d --build
