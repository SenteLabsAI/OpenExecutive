# Microsoft 365 MCP server (@softeria/ms-365-mcp-server) — a Node package, so
# it is installed in a throwaway Node stage and only the runtime bits are copied
# into the Python API image below (no npm/apt-node in the final image).
# `--omit=optional` skips keytar (a native OS-keychain binding that needs
# libsecret and a build toolchain) and the Azure Key Vault SDKs; the launcher
# runs with MS365_MCP_USE_KEYTAR=0 so the server uses its file-backed token
# store on the /data volume instead. Pinned: the tool names the gateway's
# egress gate keys on (_GATED_M365_* in orchestrator/mcp_gateway.py) and the
# argument shapes workspace/microsoft.py sends are verified against this exact
# version — bump them together.
FROM node:20-bookworm-slim AS ms365-build
RUN npm install -g --omit=optional --no-audit --no-fund \
    "@softeria/ms-365-mcp-server@0.154.2"

# Python job sandbox (workflows/python_job.py): Pyodide (Python in
# WebAssembly) run by Deno, which grants it read access to /opt/pysandbox only:
# no network, environment, writes or processes. Both come from npm; the
# compiled libraries jobs may import come from Pyodide's CDN, each checked
# against the sha256 in Pyodide's pyodide-lock.json. Pinned together: the lock
# names the wheels this exact Pyodide can load.
FROM node:20-bookworm-slim AS pysandbox-build
ARG PYODIDE_VERSION=314.0.7
ARG DENO_VERSION=2.9.6
WORKDIR /build
COPY docker/pysandbox-fetch.mjs ./
RUN npm install --no-audit --no-fund "pyodide@${PYODIDE_VERSION}" "deno@${DENO_VERSION}" \
    && mkdir -p /opt/pysandbox/wheels \
    && cp -L node_modules/deno/deno /opt/pysandbox/deno \
    && cp -r node_modules/pyodide /opt/pysandbox/pyodide \
    && node pysandbox-fetch.mjs /opt/pysandbox/pyodide \
       "https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full" \
       numpy pandas matplotlib pillow lxml

FROM python:3.11-slim AS api

WORKDIR /app

# git is required by the MCP gateway: it launches `extensible-mcp` via
# `uvx --from git+...`, which clones the repo. Without git, MCP startup
# fails with "Git executable not found" and uvicorn exits before the
# health check passes.
# curl is required by the health check in docker/docker-compose.yml
# (`curl -f http://localhost:8000/health`). python:3.11-slim does not ship it,
# so that check failed on every run and compose marked a working API
# unhealthy. git already pulls in libcurl, so this adds only the binary.
# libstdc++6 is what the copied-in Node binary (ms365 stage below) links
# against; python:*-slim does not ship it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

# Pre-warm the extensible-mcp gateway so the API does not have to clone
# from GitHub on every cold start. We use the EXACT invocation the
# gateway uses at runtime (`uvx --from git+...@<commit>`) — `uv tool install`
# populates a different cache and does not help here. The arguments must match
# _EXTENSIBLE_MCP_LAUNCH_ARGS in orchestrator/mcp_gateway.py (a unit test
# enforces it): the commit pins extensible-mcp's code and --exclude-newer pins
# the resolution of its dependencies, so the cache holds exactly what the
# runtime launches and the gateway can start with no network. That holds only
# if this step succeeded: `|| true` keeps the build green if GitHub is briefly
# unreachable, and such an image fetches the commit — and needs network — on
# its first gateway start. `uv` itself (installed above) is not pinned.
RUN uvx --exclude-newer 2026-09-23T00:00:00Z \
        --from git+https://github.com/SenteLabsAI/extensible-mcp@6862ebd29c95a1e40b929226637d38b5a5659c80 extensible-mcp --help \
    || true

COPY packages/core/pyproject.toml packages/core/uv.lock ./
# Install exactly what uv.lock pins, straight from the lock. `uv sync` honours
# the lock's per-package index (torch comes from PyTorch's CPU-only index,
# everything else from PyPI — see [tool.uv.sources] in pyproject.toml) and
# verifies the lock's hashes, so no index or strategy flags are needed here.
# The previous `uv export` + `uv pip install -r` shape could not do this:
# the export carries no index URLs, and adding the CPU index at install time
# made uv fetch every package that mirror also hosts (numpy, sympy, jinja2,
# requests, ...) from download.pytorch.org instead of PyPI.
# `--locked` (NOT `--frozen`, which skips the check) fails the build if
# uv.lock is stale relative to pyproject.toml — run `uv lock` and commit.
# `--no-install-project`: the source is not in this layer yet (and this keeps
# README.md out of it — copying it here busts the cache for the full dep
# install and the baked models below on every README edit, #81); the project
# itself is registered further down. `--inexact` stops uv removing packages
# the lock does not know about — at this point that is `uv` itself (pip-
# installed above) plus pip and setuptools. UV_PROJECT_ENVIRONMENT=/usr/local targets
# the system interpreter's prefix, so the packages land in system
# site-packages exactly as before: the model bakes below and the runtime CMD
# rely on that.
# (The fastembed install further down is a separate, unpinned resolution into
# the same env; today it moves nothing because the locked versions already
# satisfy its constraints.)
RUN UV_PROJECT_ENVIRONMENT=/usr/local \
    uv sync --locked --no-dev --no-install-project --inexact

# Bake the embedding model the MCP gateway loads at startup
# (sentence-transformers/all-MiniLM-L6-v2). Without this the ~90 MB model is
# fetched from Hugging Face on every cold start, which dominates boot time —
# see the health-check grace period note in docs/deployment.md. extensible-mcp is
# launched via `uvx` and inherits this process's environment, so pointing
# HF_HOME at an image path lets that subprocess load the model from disk
# instead of the network. Runs right after the dependency install so the
# layer is reused unless pyproject/uv.lock change, and before the offline flags
# (set further down) so the build itself can still reach the Hub.
ENV HF_HOME=/opt/hf-cache
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

# Bake the model the MCP gateway ACTUALLY loads at startup. The gateway proxies
# extensible-mcp (launched via uvx), whose tool-search index uses fastembed —
# NOT sentence-transformers. fastembed silently remaps the name
# "sentence-transformers/all-MiniLM-L6-v2" to the ONNX repo
# Qdrant/all-MiniLM-L6-v2-onnx and downloads it on every cold start unless its
# cache is already populated. fastembed lives only in the runtime uvx tool env
# (not the system env), so install it here purely to warm the cache.
# fastembed's define_cache_dir() reads FASTEMBED_CACHE_PATH; baking with the same
# value orchestrator/mcp_gateway.py forwards to the subprocess at runtime lets it
# load the model from disk (offline, see HF_HUB_OFFLINE below) instead of the
# network. No `|| true`: if the bake can't fetch, fail the build rather than ship
# an image that silently re-downloads — the model is deterministic.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed-cache
RUN uv pip install --system "fastembed>=0.6.0" \
    && python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"

# Google Workspace MCP server, run co-located as a stdio child of the MCP gateway
# (see docker/workspace-mcp-launch.sh + packages/core/mcp_servers.json.example).
# Installed into its OWN venv — NOT the system env — so its deps (fastmcp etc.)
# can't collide with the API's. Pinned to 1.29.0: it has the
# `--tool-tier {core,extended,complete}` flag the launcher uses (1.3.0 did NOT —
# it only had `--tools`, so `--tool-tier complete` errored and the child died),
# and its fastmcp (4.x) works with modern pydantic (no pin needed).
# The build-time smoke runs the EXACT flag combo the launcher uses
# (`--tool-tier complete`): if a future bump removes the flag or breaks the
# import, the BUILD fails loudly instead of crash-looping in production.
# The pin is deliberate — a new workspace-mcp can rename tools/args that the
# gateway's egress gates key on (_GATED_GMAIL_TOOLS / _GATED_CALENDAR_TOOLS /
# Drive share gate in orchestrator/mcp_gateway.py). Bump together with those gates.
RUN uv venv /opt/workspace-mcp \
    && uv pip install --python /opt/workspace-mcp/bin/python "workspace-mcp==1.29.0" \
    && /opt/workspace-mcp/bin/workspace-mcp --tool-tier complete --help >/dev/null 2>&1
COPY docker/workspace-mcp-launch.sh /usr/local/bin/workspace-mcp-launch.sh
RUN chmod +x /usr/local/bin/workspace-mcp-launch.sh

# Microsoft 365 MCP server, run co-located as a stdio child of the MCP gateway
# (see docker/ms365-mcp-launch.sh + packages/core/mcp_servers.json.example) —
# the Outlook analogue of the workspace-mcp block above. Only the Node binary
# and the installed package come across from the ms365-build stage; the
# /usr/local/bin/ms-365-mcp-server wrapper is written here (the package's own
# bin symlink is relative to the Node image's layout). The build-time smoke
# runs the LAUNCHER ITSELF with `--list-permissions` (a throwaway client id and
# credentials dir), i.e. the exact `--enabled-tools` allow-list the gateway
# will start it with, and loads the whole tool registry: if a future bump
# renames a tool, drops a flag, or fails to import, the BUILD fails loudly
# instead of crash-looping in production.
COPY --from=ms365-build /usr/local/bin/node /usr/local/bin/node
COPY --from=ms365-build /usr/local/lib/node_modules/@softeria /usr/local/lib/node_modules/@softeria
COPY docker/ms365-mcp-launch.sh /usr/local/bin/ms365-mcp-launch.sh
# The OneDrive sync's token helper (`ms365-mcp-launch.sh --access-token`). The
# smoke below fails the build if a package bump removes the AuthManager calls
# it relies on.
COPY docker/ms365-access-token.mjs /usr/local/bin/ms365-access-token.mjs
RUN printf '#!/bin/sh\nexec /usr/local/bin/node /usr/local/lib/node_modules/@softeria/ms-365-mcp-server/dist/index.js "$@"\n' \
        > /usr/local/bin/ms-365-mcp-server \
    && chmod +x /usr/local/bin/ms-365-mcp-server /usr/local/bin/ms365-mcp-launch.sh \
    && MS365_MCP_CLIENT_ID=00000000-0000-0000-0000-000000000000 \
       MS365_MCP_CREDENTIALS_DIR=/tmp/ms365-smoke \
       /usr/local/bin/ms365-mcp-launch.sh --list-permissions >/dev/null \
    && rm -rf /tmp/ms365-smoke \
    && node --input-type=module -e "const m = await import('/usr/local/bin/ms365-access-token.mjs'); const A = await m.loadAuthManager(); for (const f of ['loadTokenCache', 'getCurrentAccount']) if (typeof A.prototype[f] !== 'function') throw new Error('AuthManager.' + f + ' is missing'); if (typeof A.create !== 'function') throw new Error('AuthManager.create is missing');"

# The Python job sandbox, plus the pure-Python libraries jobs get on every run
# (pypdf, openpyxl). Pinned; the smoke after `uv pip install` below runs a job
# that imports them, pandas and matplotlib, with the same Deno permissions the
# API uses, so a broken sandbox fails the build.
COPY --from=pysandbox-build /opt/pysandbox /opt/pysandbox
RUN pip download --no-deps --only-binary=:all: --dest /opt/pysandbox/wheels \
        "pypdf==6.19.0" "openpyxl==3.1.5" "et_xmlfile==2.0.0"

COPY packages/core/ .

# Demo fixtures live at the repo root, not inside packages/core/. Copy them
# into the image at /app/fixtures/ so the fixture loader's walk-up search
# (looking for a sibling `fixtures/` directory) succeeds inside the container.
COPY fixtures/ ./fixtures/

# Register the `openexecutive` console script (defined in
# [project.scripts] in pyproject.toml) so admin commands like
# `openexecutive ingest-oer` are runnable via a shell on the container.
# Dependencies were already installed in the earlier layer, so --no-deps
# keeps this fast and cache-friendly.
RUN uv pip install --system --no-deps .

RUN W=/app/openexecutive/workflows/python_job_worker.mjs \
    && WHEELS=$(ls /opt/pysandbox/wheels/*.whl | sed 's/.*/"&"/' | paste -sd, -) \
    && printf '{"code":"import pypdf, openpyxl, pandas, matplotlib\\nstr(len(pypdf.__version__) > 0)","pyodide":"/opt/pysandbox/pyodide","wheels":[%s],"files":{}}' "$WHEELS" \
       | env -i PATH=/usr/bin:/bin HOME=/tmp DENO_DIR=/tmp/deno-smoke DENO_NO_UPDATE_CHECK=1 \
         /opt/pysandbox/deno run --no-prompt --quiet --no-remote \
         --allow-read=/opt/pysandbox,$W $W | grep -q '"result":"True"' \
    && rm -rf /tmp/deno-smoke

ENV PYTHONUNBUFFERED=1
ENV VECTOR_STORE_PATH=/data/chroma_db
ENV COMPANY_PROFILE_PATH=/data/company/profile.yaml
# Both embedding models were baked into the image above (sentence-transformers
# under $HF_HOME, fastembed under $FASTEMBED_CACHE_PATH), so force offline mode at
# runtime: this skips the per-boot HEAD/etag round-trip to the Hub and makes cold
# starts independent of network to Hugging Face. HF_HUB_OFFLINE is what fastembed
# (via huggingface_hub) honors and is forwarded into the extensible-mcp subprocess
# by orchestrator/mcp_gateway.py. If a baked cache were ever missing the model,
# startup fails fast rather than silently re-downloading — acceptable since the
# models are baked deterministically.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

# Defaults for the co-located workspace-mcp child. The credentials dir lives on
# the /data volume so an OAuth token survives restarts; a deployment sets the
# same values (and the per-install GWORKSPACE_AUTH_MODE / Google secrets).
ENV WORKSPACE_MCP_CREDENTIALS_DIR=/data/google_credentials
ENV WORKSPACE_MCP_TOOL_TIER=complete
# The Google services workspace-mcp loads: "all", or comma-separated names
# (passed as --tools by docker/workspace-mcp-launch.sh).
ENV WORKSPACE_MCP_TOOLS=all
# Act as me: each person's own-Gmail credential (scripts/connect-own-gmail.py),
# on the volume too, and deliberately NOT inside the workspace-mcp dir above.
ENV DELEGATION_GOOGLE_CREDENTIALS_DIR=/data/delegation_google

# Defaults for the co-located Microsoft 365 MCP child. The MSAL token cache
# lives on the /data volume so the one-time device-code login survives
# restarts; keytar (OS keychain) is off because the container has none and the
# native module is not installed. Per-install identity (MS365_MCP_CLIENT_ID,
# MS365_MCP_TENANT_ID, MS365_MCP_EXPECTED_USERNAME) is set by the deployment.
ENV MS365_MCP_CREDENTIALS_DIR=/data/ms365_credentials
ENV MS365_MCP_USE_KEYTAR=0

EXPOSE 8000

CMD ["uvicorn", "openexecutive.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
