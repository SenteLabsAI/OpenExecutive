FROM python:3.11-slim AS api

WORKDIR /app

# git is required by the MCP gateway: it launches `extensible-mcp` via
# `uvx --from git+...`, which clones the repo. Without git, MCP startup
# fails with "Git executable not found" and uvicorn exits before the
# health check passes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

# Pre-warm the extensible-mcp gateway so the API does not have to clone
# from GitHub on every cold start. We use the EXACT invocation the
# gateway uses at runtime (`uvx --from git+...`) — `uv tool install`
# populates a different cache and does not help here. `|| true` keeps
# the build green if GitHub is briefly unreachable; uvx will retry at
# runtime.
RUN uvx --from git+https://github.com/SenteLabsAI/extensible-mcp extensible-mcp --help \
    || true

COPY packages/core/pyproject.toml packages/core/uv.lock* ./
RUN uv export --quiet --frozen --no-dev --no-emit-project --format requirements-txt \
        --output-file /tmp/requirements.txt \
    && uv pip install --system --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Chroma's default embedding function uses a separate ONNX cache under
# /root/.cache/chroma. Warm it in addition to the sentence-transformers and
# fastembed caches below; otherwise every fresh container downloads ~79 MB
# before the API can become healthy.
RUN python -c "from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2; ONNXMiniLM_L6_V2()(['docker cache warmup'])"

# Bake the embedding model the MCP gateway loads at startup
# (sentence-transformers/all-MiniLM-L6-v2). Without this the ~90 MB model is
# fetched from Hugging Face on every cold start, which dominates boot time —
# see the health-check grace period note in fly.api.toml. extensible-mcp is
# launched via `uvx` and inherits this process's environment, so pointing
# HF_HOME at an image path lets that subprocess load the model from disk
# instead of the network. Runs right after the dependency install so the
# layer is reused unless pyproject changes, and before the offline flags
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
# can't collide with the API's. Pinned to 1.21.1: this version has the
# `--tool-tier {core,extended,complete}` flag the launcher uses (1.3.0 did NOT —
# it only had `--tools`, so `--tool-tier complete` errored and the child died),
# and it ships fastmcp 3.4.2 which works with modern pydantic (no pin needed).
# The build-time smoke runs the EXACT flag combo the launcher uses
# (`--tool-tier complete`): if a future bump removes the flag or breaks the
# import, the BUILD fails loudly instead of crash-looping in production.
# The pin is deliberate — a new workspace-mcp can rename tools/args that the
# gateway's egress gates key on (_GATED_GMAIL_TOOLS / _GATED_CALENDAR_TOOLS /
# Drive share gate in orchestrator/mcp_gateway.py). Bump together with those gates.
RUN uv venv /opt/workspace-mcp \
    && uv pip install --python /opt/workspace-mcp/bin/python "workspace-mcp==1.21.1" \
    && /opt/workspace-mcp/bin/workspace-mcp --tool-tier complete --help >/dev/null 2>&1
COPY docker/workspace-mcp-launch.sh /usr/local/bin/workspace-mcp-launch.sh
RUN chmod +x /usr/local/bin/workspace-mcp-launch.sh

COPY packages/core/ .

# Demo fixtures live at the repo root, not inside packages/core/. Copy them
# into the image at /app/fixtures/ so the fixture loader's walk-up search
# (looking for a sibling `fixtures/` directory) succeeds inside the container.
COPY fixtures/ ./fixtures/

# Register the `openexecutive` console script (defined in
# [project.scripts] in pyproject.toml) so admin commands like
# `openexecutive ingest-oer` are runnable via `flyctl ssh console`.
# Dependencies were already installed in the earlier layer, so --no-deps
# keeps this fast and cache-friendly.
RUN uv pip install --system --no-deps .

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
# the /data volume so an OAuth token survives restarts; fly.api.toml [env] sets
# the same values (and the per-install GWORKSPACE_AUTH_MODE / Google secrets).
ENV WORKSPACE_MCP_CREDENTIALS_DIR=/data/google_credentials
ENV WORKSPACE_MCP_TOOL_TIER=complete

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"]

CMD ["uvicorn", "openexecutive.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
