#!/usr/bin/env sh
# Launcher for the Google Workspace MCP server (workspace-mcp), run as a STDIO
# child of the API's MCP gateway (extensible-mcp). The Executive consumes this
# server's Gmail/Calendar/Drive tools through that gateway.
#
# It is co-located in the API image (not a separate service): the gateway spawns
# it on demand from /data/company/mcp_servers.json, which points `command` here.
# See docs/deployment.md → "Google Workspace (co-located in the API)".
#
# Two Google-auth modes, selected per install via GWORKSPACE_AUTH_MODE (an env
# var — no image rebuild to switch):
#
#   oauth            (default) Single-user OAuth. The exec account grants consent
#                    once; the refresh token persists under
#                    WORKSPACE_MCP_CREDENTIALS_DIR on the API's /data volume.
#   service_account  Domain-wide delegation. A Workspace service account
#                    impersonates USER_GOOGLE_EMAIL. No browser callback.
#
# These vars reach this script because the gateway forwards them to extensible-mcp
# (_FORWARDED_ENV_VARS in orchestrator/mcp_gateway.py) and the google_workspace
# entry's `env` block passes them on to this child.
set -eu

AUTH_MODE="${GWORKSPACE_AUTH_MODE:-oauth}"
TOOL_TIER="${WORKSPACE_MCP_TOOL_TIER:-complete}"
# Which Google services to load: comma-separated service names, or "all"
# (the default). Passed to workspace-mcp as --tools.
SERVICES="${WORKSPACE_MCP_TOOLS:-all}"

# extensible-mcp leaves a `$VAR` in the config's env block as that literal text
# when the API has no such variable, so treat an unexpanded value as unset. So is
# a value with no service names in it (",").
case "$TOOL_TIER" in '$'*) TOOL_TIER=complete ;; esac
case "$SERVICES" in '$'*) SERVICES=all ;; esac
SERVICES=$(printf '%s' "$SERVICES" | tr ',' ' ')
case "$SERVICES" in *[![:space:]]*) ;; *) SERVICES=all ;; esac
# The choice reaches workspace-mcp only as --tools below. Left in the
# environment it is read again as a fallback, and "all" is not a service name.
unset WORKSPACE_MCP_TOOLS

# CRITICAL (co-location): in stdio single-user mode workspace-mcp starts a
# "minimal OAuth server" on WORKSPACE_MCP_PORT, which DEFAULTS TO 8000 — the same
# port the co-located API (uvicorn) binds. Left unset, that helper grabs 8000
# first and uvicorn dies with "address already in use", hanging the whole boot.
# Pin it to a free non-8000 port so the API keeps 8000. stdio transport itself
# uses no port; this only relocates the helper (whose bind is best-effort — token
# refresh uses the stored credential, not this server). Overridable if 8765 ever
# clashes with something else in the container.
export WORKSPACE_MCP_PORT="${WORKSPACE_MCP_PORT:-8765}"

# Token/credential store lives on the mounted volume so OAuth grants survive
# restarts. Harmless (and empty) in service_account mode. Lock it to the owner
# (0700) — it holds the OAuth refresh token — as defense-in-depth even though
# /data is a single-tenant private volume.
if [ -n "${WORKSPACE_MCP_CREDENTIALS_DIR:-}" ]; then
    mkdir -p "$WORKSPACE_MCP_CREDENTIALS_DIR"
    chmod 700 "$WORKSPACE_MCP_CREDENTIALS_DIR"
fi

# STDIO transport (the gateway speaks MCP over stdio to this child) — note this
# is workspace-mcp's default, so we do NOT pass --transport. Tier is explicit.
# Invoke the binary from its isolated venv (docker/Dockerfile) so its fastmcp/
# pydantic deps don't collide with the API's system env.
set -- /opt/workspace-mcp/bin/workspace-mcp --tool-tier "$TOOL_TIER"
if [ "$SERVICES" != all ]; then
    # Split on whitespace into one argument per service, with globbing off so a
    # name is never expanded against the filesystem.
    set -f
    # shellcheck disable=SC2086
    set -- "$@" --tools $SERVICES
    set +f
fi

case "$AUTH_MODE" in
    oauth)
        # Single-user mode: tools accept a user_google_email argument (exactly
        # how the Executive's typed Gmail/Calendar tools already call them) and
        # the server avoids the per-session OAuth 2.1 handshake.
        set -- "$@" --single-user
        ;;
    service_account)
        # workspace-mcp auto-detects service-account auth from the key env vars;
        # fail fast with a clear message if the operator forgot to set them
        # rather than letting the server start and 401 on every call.
        if [ -z "${GOOGLE_SERVICE_ACCOUNT_KEY_FILE:-}" ] \
           && [ -z "${GOOGLE_SERVICE_ACCOUNT_KEY_JSON:-}" ]; then
            echo "GWORKSPACE_AUTH_MODE=service_account requires GOOGLE_SERVICE_ACCOUNT_KEY_FILE or GOOGLE_SERVICE_ACCOUNT_KEY_JSON" >&2
            exit 64
        fi
        if [ -z "${USER_GOOGLE_EMAIL:-}" ]; then
            echo "GWORKSPACE_AUTH_MODE=service_account requires USER_GOOGLE_EMAIL (the delegated user to impersonate)" >&2
            exit 64
        fi
        ;;
    *)
        echo "Unknown GWORKSPACE_AUTH_MODE='$AUTH_MODE' (expected 'oauth' or 'service_account')" >&2
        exit 64
        ;;
esac

exec "$@"
