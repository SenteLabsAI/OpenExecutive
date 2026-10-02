#!/usr/bin/env sh
# Launcher for the Microsoft 365 MCP server (@softeria/ms-365-mcp-server), run
# as a STDIO child of the API's MCP gateway (extensible-mcp). The Executive
# consumes this server's Outlook mail + calendar tools through that gateway —
# the Microsoft analogue of docker/workspace-mcp-launch.sh.
#
# Co-located in the API image (not a separate service): the gateway spawns it
# on demand from /data/company/mcp_servers.json, which points `command` here.
# See docs/deployment.md → "Microsoft 365 credentials".
#
# Auth is MSAL device-code (delegated, single mailbox). The refresh token
# persists under MS365_MCP_CREDENTIALS_DIR on the API's /data volume so a
# one-time `--login` survives restarts. All of these vars reach this script
# because the gateway forwards them to extensible-mcp (_FORWARDED_ENV_VARS in
# orchestrator/mcp_gateway.py) and the microsoft_365 entry's `env` block passes
# them on to this child.
set -eu

# extensible-mcp resolves the `$VAR` placeholders in the config's `env` block
# from the API's environment — and leaves an UNSET one as the literal string
# "$VAR". Left alone, that literal would pass the "is it empty?" checks below
# and reach MSAL as a bogus client id / tenant ("$MS365_MCP_TENANT_ID"), which
# fails only at the first Graph call with an authority error nobody can read.
# Scrub any such placeholder back to "unset" before deciding anything.
for var in MS365_MCP_CLIENT_ID MS365_MCP_TENANT_ID MS365_MCP_CLIENT_SECRET \
           MS365_MCP_EXPECTED_USERNAME MS365_MCP_ORG_MODE MS365_MCP_OAUTH_TOKEN \
           MS365_MCP_CREDENTIALS_DIR MS365_MCP_TOKEN_CACHE_PATH \
           MS365_MCP_SELECTED_ACCOUNT_PATH MS365_MCP_USE_KEYTAR MS365_MCP_PRESET \
           MS365_MCP_ENABLED_TOOLS; do
    eval "val=\${$var:-}"
    case "$val" in
        \$*) unset "$var" ;;
    esac
done

# The server ships a built-in (Softeria-owned) public client id it would fall
# back to. Refuse that: consent, audit and tenant policy for the Executive's
# mailbox belong to the operator's own Entra app registration.
if [ -z "${MS365_MCP_CLIENT_ID:-}" ]; then
    echo "microsoft_365 requires MS365_MCP_CLIENT_ID (the Entra app registration's Application (client) ID)" >&2
    exit 64
fi

# Token cache + selected-account file live on the mounted volume so the
# device-code grant survives restarts. Lock the directory to the owner (0700):
# it holds the MSAL refresh token. The server writes its own AES key next to
# the cache as .cache-key when the OS keychain is off (below).
CRED_DIR="${MS365_MCP_CREDENTIALS_DIR:-/data/ms365_credentials}"
mkdir -p "$CRED_DIR"
chmod 700 "$CRED_DIR"
export MS365_MCP_TOKEN_CACHE_PATH="${MS365_MCP_TOKEN_CACHE_PATH:-$CRED_DIR/.token-cache.json}"
export MS365_MCP_SELECTED_ACCOUNT_PATH="${MS365_MCP_SELECTED_ACCOUNT_PATH:-$CRED_DIR/.selected-account.json}"
# No OS keychain in a container: keytar (a native optional dependency) is not
# installed in the image, and the server's file-backed store is what we want
# on the volume anyway. Accepted "off" values: 0 / false / no / off.
export MS365_MCP_USE_KEYTAR="${MS365_MCP_USE_KEYTAR:-0}"

# STDIO transport is the server's default, so no --http. Unlike workspace-mcp
# it binds no helper port in stdio mode, so there is no port-clash guard here.
#
# Tool surface: an EXPLICIT allow-list (--enabled-tools, an anchored regex of
# tool names), not the upstream `mail,calendar` preset. The preset also
# registers tools that can address people the gateway's roster gate does not
# see — forward-calendar-event, create-/update-mail-rule (silent inbox
# forwarding), update-mailbox-settings (external auto-reply), calendar
# permission sharing, whole-calendar delete — and an upstream bump can add
# more. The list below is exactly the Outlook mail + calendar tools the
# gateway gates or that carry no recipient; the scopes requested at --login
# follow from it (Mail.ReadWrite, Mail.Send, Calendars.ReadWrite). The
# server's account tools (login, logout, verify-login, list-accounts,
# select-account, remove-account) are always registered regardless of the
# filter; the gateway refuses login / logout / select-account /
# remove-account itself (orchestrator/mcp_gateway.py).
# Override with MS365_MCP_ENABLED_TOOLS (a regex) or MS365_MCP_PRESET (an
# upstream preset name) only if you also extend the gate in
# orchestrator/mcp_gateway.py — re-verify with --list-permissions.
DEFAULT_ENABLED_TOOLS='^(list-mail-folders|list-mail-folder-messages|list-mail-messages|get-mail-message|list-mail-attachments|download-bytes|send-mail|reply-mail-message|reply-all-mail-message|forward-mail-message|create-draft-email|create-reply-draft|create-reply-all-draft|create-forward-draft|update-mail-message|send-draft-message|move-mail-message|list-calendars|list-calendar-events|get-calendar-event|create-calendar-event|update-calendar-event|delete-calendar-event|cancel-calendar-event|accept-calendar-event|decline-calendar-event|tentatively-accept-calendar-event|get-calendar-view)$'
#
# The script's own arguments stay at the END of the command line so the
# seeding path works with the exact environment the gateway will use:
#   ms365-mcp-launch.sh --login          (device code: prints URL + code)
#   ms365-mcp-launch.sh --verify-login
#   ms365-mcp-launch.sh --list-permissions
# With no extra args this execs the stdio server, as the gateway expects.

# Pin the account the cached token may belong to. The env var is honoured
# natively too; the flag makes the pin visible in `ps` and survives an
# upstream env-name change.
if [ -n "${MS365_MCP_EXPECTED_USERNAME:-}" ]; then
    set -- --expected-username "$MS365_MCP_EXPECTED_USERNAME" "$@"
fi

if [ -n "${MS365_MCP_PRESET:-}" ]; then
    set -- --preset "$MS365_MCP_PRESET" "$@"
else
    set -- --enabled-tools "${MS365_MCP_ENABLED_TOOLS:-$DEFAULT_ENABLED_TOOLS}" "$@"
fi
set -- "${MS365_MCP_SERVER_BIN:-/usr/local/bin/ms-365-mcp-server}" "$@"

exec "$@"
