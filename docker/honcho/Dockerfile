# Self-hosted Honcho for Open Executive.
#
# Mirrors plastic-labs/honcho's official Dockerfile (Python 3.13-slim,
# uv 0.9.24) but pins the Honcho source to a specific release tag, bakes
# in OE's config.toml (Anthropic direct for every LLM slot, dreaming on,
# AUTH_USE_AUTH=true), and bundles an embeddings sidecar
# (`docker/honcho/embed_server.py`) that runs as the `embed` process
# serving `BAAI/bge-small-en-v1.5` locally via fastembed/ONNX so
# embeddings never leave the deployment's private network.
#
# Build context is the repo root (so this file references
# docker/honcho/config.toml + embed_server.py from cwd). See
# README.md for the three process roles and which command
# each one runs.
# Honcho requires Python >= 3.13 since v3.1.1 (`requires-python` in its
# pyproject), and its own image is python:3.13-slim. Match it so the
# system interpreter satisfies the lockfile (see UV_PYTHON_PREFERENCE below).
FROM python:3.13-slim-bookworm

# Bump deliberately — Honcho ships database migrations between minors and
# the SDK's wire format can shift. The runbook (README.md)
# covers the upgrade procedure.
ARG HONCHO_VERSION=v3.2.0

# Pinned to the same uv release Honcho's own Dockerfile uses, so the
# uv.lock resolves byte-identically.
COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /uvx /bin/

# git: needed to fetch the pinned Honcho source.
# ca-certificates: required at runtime for outbound HTTPS to Anthropic.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Shallow clone at the pinned tag, then drop .git to keep the image lean.
RUN git clone --depth 1 --branch "${HONCHO_VERSION}" \
        https://github.com/plastic-labs/honcho.git . \
    && rm -rf .git

# Use the base image's system Python instead of letting uv download its own.
# Otherwise uv stashes a private Python under /root/.local/share/uv/python/...
# and the venv's python symlink dead-ends after we USER-switch to `app`
# because /root is mode 700. Pinning to the system interpreter keeps the
# whole venv self-contained under /app, which `chown -R app:app /app`
# below can then hand off cleanly. With `only-system`, the base image's
# Python *must* satisfy Honcho's `requires-python`, so the base image
# above moves in lockstep with the pinned Honcho release.
ENV UV_PYTHON_PREFERENCE=only-system \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Frozen install matches Honcho's lockfile; --no-dev skips test/lint deps.
RUN uv sync --frozen --no-dev

# Embeddings sidecar deps: fastembed wraps ONNX-runtime models with no
# torch dependency, so the marginal image size is small (~130MB for
# bge-small-en-v1.5 weights + onnxruntime). Installed into Honcho's venv
# so the same `embed` process can `import fastembed` directly.
RUN uv pip install --python /app/.venv/bin/python "fastembed>=0.6.0,<1"

# Overlay OE's Honcho config and the embeddings sidecar source.
COPY docker/honcho/config.toml /app/config.toml
COPY docker/honcho/embed_server.py /app/embed_server.py

# Pre-download the default embedding model so the first /v1/embeddings
# call doesn't pay a ~10s cold-start fetch. fastembed caches models
# under HOME/.cache/fastembed by default; we'll chown that path along
# with /app after the next step.
ENV FASTEMBED_CACHE_PATH=/app/.cache/fastembed
RUN /app/.venv/bin/python -c \
        "from fastembed import TextEmbedding; \
         TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='/app/.cache/fastembed')"

# Drop privileges. Matches Honcho's official image's `app` user.
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

# Put the uv-managed venv on PATH so each process's command can
# invoke `fastapi` / `python` directly without an `uv run` prefix
# (matches Honcho's upstream CMD).
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000 8001

# Default command is the API. Override it per process (api / deriver / embed)
# — see README.md.
CMD ["fastapi", "run", "--host", "0.0.0.0", "src/main.py"]
