.PHONY: dev stop test lint eval docker clean install discord

install:
	cd packages/core && uv sync
	cd packages/ui && npm install

# Both dev recipe lines source the repo-root .env into the process env:
# - the UI needs it because Next.js only auto-loads packages/ui/.env*, so
#   Auth.js never saw AUTH_SECRET etc. (issue #44);
# - the API needs it because BACKEND_SHARED_SECRET / BACKEND_ALLOWED_ORIGINS
#   are read from os.environ, not pydantic Settings — dotenv alone doesn't
#   surface them, silently leaving the API gate open.
# Note: exported values win over packages/ui/.env.local for duplicate keys.
# .env values must be shell-safe: quote anything containing spaces or `$`.
#
# One-person mode: with AUTH_GOOGLE_ID blank there is no sign-in, so the UI
# binds to 127.0.0.1 (nobody else on the network can reach it) and gets
# OE_LOCAL_OWNER_MODE=1, which packages/ui/src/auth.ts requires before it
# offers the "Open" button. The flag and the loopback bind must travel
# together — the flag alone would hand the owner's seat to the whole network —
# so this recipe is the only thing that sets it. A blank AUTH_SECRET gets a
# throwaway one for the run (you click "Open" again after a restart).
dev:
	@echo "Starting Open Executive..."
	@[ -d packages/ui/node_modules ] || { echo "Installing the web app's packages (first run only)..."; cd packages/ui && npm install; }
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; cd packages/core && uv run uvicorn openexecutive.api.main:app --reload --port 8000 &
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; cd packages/ui && \
	if [ -n "$$AUTH_GOOGLE_ID" ]; then exec npm run dev; fi; \
	echo "Google sign-in is not set up, so this runs in one-person mode: open http://localhost:3000 on this computer."; \
	[ -n "$$AUTH_SECRET" ] || AUTH_SECRET=$$(node -e "process.stdout.write(require('crypto').randomBytes(32).toString('base64'))"); \
	AUTH_SECRET="$$AUTH_SECRET" AUTH_TRUST_HOST="$${AUTH_TRUST_HOST:-true}" OE_LOCAL_OWNER_MODE=1 exec npm run dev -- -H 127.0.0.1

stop:
	@lsof -ti :8000 -ti :3000 2>/dev/null | xargs kill -9 2>/dev/null || true
	@echo "Stopped."

test:
	cd packages/core && uv run pytest tests/ -v --tb=short

lint:
	cd packages/core && uv run ruff check openexecutive/ && uv run mypy openexecutive/

eval:
	cd packages/core && uv run python ../../evals/run_evals.py \
		--scenarios ../../evals/scenarios/ \
		--output ../../evals/results/

# --env-file makes ${VAR} interpolation in docker-compose.yml read the
# repo-root .env (compose only auto-reads docker/.env otherwise). The
# containers additionally load the full .env via each service's env_file.
COMPOSE_ENV_FILE := $(if $(wildcard .env),--env-file .env,)

docker:
	docker compose $(COMPOSE_ENV_FILE) -f docker/docker-compose.yml up --build

docker-down:
	docker compose $(COMPOSE_ENV_FILE) -f docker/docker-compose.yml down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf packages/core/.venv packages/core/.mypy_cache packages/core/.ruff_cache
	rm -rf packages/ui/node_modules packages/ui/.next

discord:
	cd packages/core && uv run python -m openexecutive.integrations.discord_bot

seed-knowledge:
	cd packages/core && uv run python -c "from openexecutive.knowledge.loader import seed_builtin_knowledge; import asyncio; asyncio.run(seed_builtin_knowledge())"
