# Self-hosted Honcho

Per-person memory ("peer memory") is backed by [Honcho](https://honcho.dev). Open
Executive talks to it over HTTP, so it works against either hosted Honcho or a
self-hosted instance — set `HONCHO_API_KEY` and `HONCHO_BASE_URL` on the API and
the application does not care which. Hosted is the simpler path; this directory
is for running your own.

## What's here

| File | Purpose |
|---|---|
| `Dockerfile` | Builds Honcho, pinned via `ARG HONCHO_VERSION` (currently `v3.2.0`), with the embedding model baked into the image |
| `config.toml` | Honcho's model configuration — which LLM serves each slot, and the embedding setup |
| `embed_server.py` | A small OpenAI-compatible embeddings server (`fastembed` + `BAAI/bge-small-en-v1.5`), so embeddings need no external vendor |

## Processes

One image, three roles — run each as its own container/process from the same build:

- **api** — Honcho's FastAPI service on `:8000`. The only one Open Executive talks to; keep it on a private network.
- **deriver** — background worker running extraction and "dreaming". Set `DERIVER_WORKERS=2`; the default of 1 falls behind bursty load.
- **embed** — the embeddings sidecar on `:8001`, reached by the deriver.

## Requirements

**Postgres with `pgvector`.** Honcho's migrations expect the extension to exist —
`CREATE EXTENSION IF NOT EXISTS vector;` before first deploy.

**Run the release steps on every deploy, before new containers serve traffic:**

```bash
python provision_db.py
python configure_embeddings.py --yes
```

`configure_embeddings.py` reconciles the pgvector column with
`EMBEDDING_VECTOR_DIMENSIONS`. Skipping it after a dimension change makes the
deriver fail every embed call with a dimension mismatch.

## Configuration

| Variable | Notes |
|---|---|
| `DATABASE_URL` | Postgres connection string |
| `AUTH_JWT_SECRET` | Every issued `HONCHO_API_KEY` derives from this — store it in a password manager; rotating it invalidates all keys |
| `LLM_ANTHROPIC_API_KEY` | Every LLM slot in `config.toml` uses Honcho's `anthropic` transport. To route through OpenRouter or another OpenAI-compatible endpoint instead, set `LLM_OPENAI_API_KEY`, `LLM_OPENAI_BASE_URL` and the per-slot `<SLOT>_MODEL_CONFIG__TRANSPORT=openai` / `__MODEL=` overrides listed at the top of `config.toml` |
| `EMBEDDING_MODEL_CONFIG__OVERRIDES__BASE_URL` | Point at the `embed` process, e.g. `http://embed:8001/v1` |
| `EMBEDDING_MODEL_CONFIG__OVERRIDES__API_KEY` | The sidecar needs no auth, but the OpenAI client requires some string |
| `EMBEDDING_VECTOR_DIMENSIONS` | `384` for `bge-small-en-v1.5`. Honcho's default schema is `Vector(1536)` |
| `DERIVER_WORKERS` | `2` |

Honcho uses nested pydantic-settings (`env_prefix="EMBEDDING_"`,
`env_nested_delimiter="__"`), so a plain `EMBEDDING_BASE_URL` is **silently
ignored**. Use the `__OVERRIDES__` form above.

## Embedding quality

The default `BAAI/bge-small-en-v1.5` (384-dim, ~130MB) scores roughly level with
OpenAI's `text-embedding-3-small` on MTEB English, while staying local and
English-only. If retrieval feels weak, climb before reaching for a vendor API:

| Step | Model | Cost |
|---|---|---|
| Default | `BAAI/bge-small-en-v1.5` (384-dim) | baseline |
| 1 | `BAAI/bge-base-en-v1.5` (768-dim) | +300MB image, +400MB RAM |
| 2 | `BAAI/bge-large-en-v1.5` (1024-dim) | +1.2GB image, +1.2GB RAM |
| 3 | A hosted embedding API (1536-dim) | a network hop and a data-residency change |

`fastembed` needs the model in its cache, which is baked at build time, so steps
1 and 2 require a rebuild — not just an env change. Any dimension change means
re-running `configure_embeddings.py`, which **alters the pgvector column and
destroys existing embeddings**. Back up first.

## Upgrading Honcho

Honcho ships breaking schema changes between minor versions. Read the
[release notes](https://github.com/plastic-labs/honcho/releases), bump
`ARG HONCHO_VERSION` in the `Dockerfile`, redeploy with the release steps above,
and smoke a peer-memory round trip before considering it done.

The base image tracks Honcho's `requires-python` (`>= 3.13` since v3.1.1). The
build installs into the system interpreter, so a release that raises the
floor needs the `FROM` line bumped in the same change.
