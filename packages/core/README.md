# Open Executive

An open-source AI system that acts as your company's virtual executive team — a senior advisor with Harvard MBA-level knowledge, customized for your specific business.

## What It Does

Open Executive provides a single coherent executive voice backed by eight specialist AI agents:

- **Chief Strategy Officer** — competitive analysis, M&A, market positioning, OKRs
- **Chief Financial Officer** — financial modeling, fundraising, unit economics, cash flow
- **Chief HR/People Officer** — hiring, compensation, performance, culture
- **General Counsel** — contracts, IP, employment law basics, compliance
- **Chief Operating Officer** — process design, vendor management, operational scaling
- **Chief Marketing Officer** — GTM strategy, brand, communications, PR
- **Chief Product Officer** — roadmap, prioritization, product strategy
- **Board Communications Director** — board decks, investor relations, governance

All responses come from one consistent executive voice. The internal agent architecture is never exposed to the user.

## Quick Start

```bash
# Clone the repo
git clone https://github.com/SenteLabsAI/OpenExecutive.git
cd OpenExecutive

# Set your Anthropic API key
cp .env.example .env
# Edit .env and add ANTHROPIC_API_KEY=sk-ant-...

# Start everything
make dev
```

Open http://localhost:3000 to start chatting with your executive.

## Onboarding Your Company

The first time you visit the app, you'll be guided through a wizard to set up your company profile:
- Company basics (name, industry, stage, team size)
- Business model and revenue
- Competitive landscape
- Strategic priorities
- Culture and values
- Optional: financial position, document upload

After onboarding, the Executive will reference your specific company context in every response.

## Interfaces

| Interface | How to Use |
|-----------|-----------|
| **Web UI** | `http://localhost:3000` |
| **Slack** | Mention `@OpenExecutive` or DM the app |
| **Email** | CC or email `exec@yourdomain.com` |
| **CLI** | `openexecutive chat` |

## Document Upload

Upload your pitch deck, financial model, strategy docs, or any company documents via the web UI or API. The Executive will reference them when relevant.

```bash
# Via CLI
openexecutive upload deck.pdf model.xlsx strategy.md

# Via API
curl -X POST http://localhost:8000/documents \
  -F "file=@deck.pdf" \
  -F "domain=strategy"
```

## Architecture

```
User message
    ↓
Executive Orchestrator (claude-sonnet-5)
    ↓ tool use → parallel specialist calls
CSO / CFO / CHRO / GC / COO / CMO / CPO / Board
    ↓ each specialist retrieves relevant context from ChromaDB
Built-in MBA knowledge + Your company documents
    ↓
Synthesized executive response
```

See [docs/architecture.md](docs/architecture.md) for the full design.

## Adding a New Specialist Agent

1. Create `packages/core/openexecutive/agents/your_agent.py` extending `BaseAgent`
2. Add a system prompt in `packages/core/openexecutive/prompts/domain_prompts.py`
3. Register the tool in `packages/core/openexecutive/orchestrator/router.py`
4. Add eval scenarios in `evals/scenarios/`
5. Submit a PR with the agent, prompt, and at least 2 eval scenarios

## Development

```bash
make dev          # Start FastAPI + Next.js
make test         # Run Python tests
make eval         # Run eval suite
make lint         # Run ruff + mypy
make docker       # Build and run Docker stack
```

## Contributing

See [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md). All PRs must include:
- Working implementation (no stubs)
- Tests for new behavior
- Eval scenarios for new agents or prompt changes

## License

Apache 2.0 — free to use commercially, requires attribution.
