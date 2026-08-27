# Docker deployment

The Compose stack runs the production FastAPI and standalone Next.js images.
The UI is exposed on port 3000, while direct API access is bound to
`127.0.0.1:8000`; browser traffic reaches the API through the authenticated UI
proxy.

## Configure

Create a repository-root environment file and fill in the blank values:

```bash
cp docker/.env.example .env
openssl rand -base64 32   # AUTH_SECRET
openssl rand -hex 32      # BACKEND_SHARED_SECRET
```

At minimum, configure:

- one model provider (`ANTHROPIC_API_KEY`, OpenRouter, or a local server)
- `EXEC_EMAIL_ADDRESS`
- `AUTH_SECRET`
- `AUTH_GOOGLE_ID` and `AUTH_GOOGLE_SECRET`
- `ALLOWED_EMAILS`
- `BACKEND_SHARED_SECRET`

For Google OAuth, register this authorized redirect URI:

```text
http://localhost:3000/api/auth/callback/google
```

If the UI is exposed under another origin, change `AUTH_URL`,
`BACKEND_ALLOWED_ORIGINS`, and the Google redirect URI together. Use HTTPS for
any non-local deployment.

## Run

```bash
make docker
```

Open <http://localhost:3000>. The first build downloads Python, Node, MCP, and
embedding-model dependencies, so it is substantially slower than later builds.
The API image is several gigabytes because the frozen ML stack and all three
embedding caches are included for network-independent container startup.

Useful operations:

```bash
make docker-logs    # follow both services
make docker-config  # render and validate the resolved Compose configuration
make docker-down    # stop containers; persistent data is retained
```

Application state is stored in the `openexecutive_executive_data` named volume:

- `/data/chroma_db` — vector indexes
- `/data/episodic_memory.db` — memory, workflows, audit records, and schedules
- `/data/company` — company profile, documents, client slots, and MCP config
- `/data/google_credentials` — optional Google Workspace MCP credentials

`make docker-down` does not delete this volume. Avoid `docker compose down -v`
unless you intentionally want to erase the Open Executive installation.

## Local model servers

Inside a container, `localhost` refers to that container. To reach Ollama, LM
Studio, or another OpenAI-compatible service on the Docker host, use the host
alias supplied by Compose:

```dotenv
LOCAL_MODELS_ENABLED=true
LOCAL_BASE_URL=http://host.docker.internal:11434/v1
LOCAL_MODELS=llama3.3
```

## Production notes

- Put a TLS reverse proxy in front of the UI and set `AUTH_URL` to its public
  HTTPS origin.
- Keep port 8000 private. The Compose mapping binds it to loopback for local
  diagnostics; remove that mapping when the API should only be reachable from
  the Compose network.
- Back up the `executive_data` volume. The API must remain single-replica until
  scheduler leader election is implemented, or scheduled actions can fire
  twice.
- Secrets are read at container start from the gitignored root `.env`; they are
  not copied into either image.
