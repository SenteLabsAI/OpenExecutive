# Mapare documentație OpenExecutive → spec

**Pin:** `ce692a21`
**Branch:** `stack/f1`
**Dată:** 20 septembrie 2026

Legendă: **Păstrăm** / **Amânăm** / **Corectăm** / **Al nostru**.

## Produs
- Un glas + 8 specialiști ascunși → **Păstrăm** (Pack 0)
- Digital twin al unui lider → **Amânăm**
- Cloud openexecutive.ai → **Nu luăm**
- Multi-client / Simulator → **Amânăm**

## Arhitectură
- Orchestrator Sonnet + consult_specialist → **Păstrăm** (nu LangGraph)
- RAG builtin + company_docs → **Păstrăm**; live_metrics în F2
- Memorie episodică SQLite → **Păstrăm**; ambient writeback off până la HITL
- Scheduler o instanță → **Păstrăm**
- Alert review cu mutări → **blocat F1** (`ALERT_REVIEW_ENABLED=false`)
- Honcho → **Amânăm**

## Canale
- Web UI OE → **Păstrăm** (designul vine de aici)
- Slack/Discord/Telegram/Gmail/Notion → **Amânăm**, token-uri unset

## Auth / deploy
- Google + ALLOWED_EMAILS → **Păstrăm**, nu e nevoie acum
- BACKEND_SHARED_SECRET + OE_PUBLIC_DEPLOYMENT → doar la deploy public
- Workspace partajat fără izolare per-user → limită acceptată

## MCP
- Fișierul mcp_servers.json pornește gateway-ul dacă MCP_ENABLED e unset → **Corectăm**: F1 forțează `MCP_ENABLED=false`
- load_mcp_server de către model → **interzis**
- fetch/github/workspace → declarate, deny-all până în F3
- Inbound MCP → **al nostru**, F3

## Env de corectat față de docs
- EXEC_EMAIL_ADDRESS: README zice optional, pydantic îl cere → identitate, nu Gmail live
- ENABLE_WEB_SEARCH: app default true, example false → noi false
- ALERT_REVIEW_ENABLED example true → noi false
- Nu copia placeholder-urile Slack din .env.example (prezența token-ului pornește botul)

## Al nostru (nu e în docs OE)
Kernel + packs, MANIFEST, policy fail-closed, guardrails 5 straturi, ledger, inbox HITL, tab-uri Settings rezervate.
