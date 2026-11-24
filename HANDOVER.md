# HANDOVER

## Status in one paragraph
Planning done, no product code yet. The repo holds the agent pipeline scaffold (CLAUDE.md, docs/BRIEF.md, `.claude/agents/*`, `.claude/commands/pipeline.md`, `.gitignore`) plus PLAN.md, TODO.md and this file, staged but not yet committed. The plan is CLI-first: Phase 0 sets up the uv/Python 3.12 project (1 h), Phase 1 builds `exit-archive export --out DIR` and proves it against the live Harvest trial account (11 h), Phase 2 adds the offline `index.html` viewer (3 h), Phase 3 the FastAPI web app with background jobs and the Stripe gate (6.5 h), Phase 4 container, CI, hardening, hosting and launch check (6 h). Total 27.5 h of the ~28 h budget. Hosting (D1) and payments (D2) are waiting for the user; nothing before T4.5 depends on them.

## What exists
- `CLAUDE.md` — project rules, commands, verified Harvest API facts
- `docs/BRIEF.md` — product brief, evidence, MVP scope, Skip list, API facts
- `.claude/agents/{orchestrator,executor,tester,deployer}.md` — agent roles
- `.claude/commands/pipeline.md` — `/pipeline` loop the main session runs
- `.gitignore` — Python caches, `.env*` (except `.env.example`), `.harvest_test`, `archives/`, `data/`, `tmp/`
- `PLAN.md` — architecture, module boundaries, data flow, token security model, archive layout, decisions, open decisions
- `TODO.md` — phased tasks with owners, estimates and acceptance criteria
- `HANDOVER.md` — this file

## How to run, test, deploy
- Install: `uv sync` (after T0.1; Python 3.12 via `uv python install 3.12` if missing)
- Test: `uv run pytest -q` · live: `uv run pytest -m live -q` (reads `~/.harvest_test`)
- Lint/types: `uv run ruff check . && uv run ruff format --check . && uv run mypy src`
- CLI (after T1.6): `HARVEST_TOKEN=... uv run exit-archive export --out ./tmp/out` — in practice `set -a; . ~/.harvest_test; set +a` first, never paste the token
- Web (after T3.2): `PAYMENT_REQUIRED=false DATA_DIR=./tmp uv run uvicorn exit_archive.web.app:app`
- Deploy: not yet; see `docs/DEPLOY.md` after T4.4/T4.5
- Push: plain `git push` over HTTPS (`gh` is not installed)

## Decisions made
- CLI and core library before the web app — prove the complete export on the live account first; the web app reuses `export_account()`.
- Python 3.12, uv, httpx async, FastAPI + Jinja2 server-rendered — brief's stack, no frontend build.
- In-process asyncio jobs, single uvicorn worker, SQLite for job metadata only — no extra services to pay for; restarts fail running jobs (buyer re-submits on the same paid session).
- Archives on local persistent disk, deleted 7 days after finishing — simplest on one machine.
- Offline viewer = static `index.html` + `data/viewer-data.js` script — `file://` cannot `fetch()` JSON.
- Token: memory only, `HarvestToken` redacted type, log redaction filter, typed safe errors, Bearer sent only to Harvest hosts, every export test asserts the token is absent from archive bytes and logs.
- Missing PDFs/receipts are recorded in `MANIFEST.missing[]`; the export does not abort.
- Full list with reasons: PLAN.md §6.

## Open decisions for the user
- D1 Hosting — recommend Render Starter + 10 GB disk (~$10/mo); alternative Fly.io (~$7/mo, needs flyctl, auto-stop off). Blocks T4.5.
- D2 Payments — recommend Stripe Payment Link at $49 with success URL `https://<host>/start?session_id={CHECKOUT_SESSION_ID}` and a restricted key; needs a Stripe account. Alternative: a merchant of record (Lemon Squeezy/Paddle) for sales tax/VAT.
- D3 Domain — optional (~$12/yr).
- D4 Multi-account tokens — recommend optional Account ID field; fail with the account list if ambiguous.
- D5 Approve privacy/data-handling page wording before launch.

## Next 3 tasks
1. T0.1 Scaffold the Python project with uv (executor)
2. T1.1 Token wrapper, safe errors and log redaction (executor)
3. T1.2 Async Harvest HTTP client (executor)

## Gotchas
- `~/.harvest_test` is `KEY=VALUE` with keys `HARVEST_TOKEN` and `HARVEST_ACCOUNT_ID` (file mode 600). Only the `live` test fixture reads it; never print it or copy it into the repo or a prompt.
- Docker daemon was not running when checked (2026-11-24); deployer must say so if `docker build` cannot be verified locally.
- `gh` CLI is not installed; CI status is checked via the GitHub REST API with `curl` or the Actions page.
- Harvest `/v2/accounts` can return Forecast accounts — filter `product == "harvest"`.
- FastAPI's default 422 response echoes the submitted `input` — must be replaced so a token is never reflected (T3.2).
- Invoice PDF URLs need no token; never attach the Bearer header to them or to any non-Harvest redirect target.
- Estimate PDF URL, rate limits on large accounts and pagination beyond page 1 are not yet verified on real data (T1.7 and Later).
- Running in WSL on `/mnt/d` (NTFS): file permission bits show as 777; do not rely on chmod in tests.

## Secrets and where they live
- `HARVEST_TOKEN`, `HARVEST_ACCOUNT_ID` — developer trial account, `~/.harvest_test` (outside the repo)
- `STRIPE_SECRET_KEY` (restricted key), `STRIPE_PAYMENT_LINK_URL` — not created yet; will live in the hosting platform's env settings
- `PUBLIC_BASE_URL`, `DATA_DIR`, `MAX_CONCURRENT_JOBS`, `PAYMENT_REQUIRED` — config, not secret; names listed in `.env.example` after T0.1/T3.2

## Last updated
2026-11-24 — last commit: none yet
