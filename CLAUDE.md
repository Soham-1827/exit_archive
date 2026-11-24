# Exit Archive

Paste a Harvest API token, get every record you ever made — time entries, clients, projects, expenses with receipt images, invoices and estimates as PDFs — as one offline, searchable archive, before your account is deleted.

## Start of every session
Read `HANDOVER.md` first, then `TODO.md`. Work happens through `/pipeline` (see `.claude/commands/pipeline.md`).

## Agents (`.claude/agents/`)
- `orchestrator` (Opus, high effort): plans, owns PLAN.md / TODO.md / HANDOVER.md, reviews, decides the next dispatch. Writes no product code.
- `executor`: implements one task, tests first.
- `tester`: quality gate, PASS or FAIL with evidence; may add tests, never product code.
- `deployer`: Docker, CI, hosting config; never spends money or deploys to production without the user's yes.
Subagents cannot start subagents, so the main session runs the orchestrator's dispatch blocks.

## Non-negotiables
- **Customer tokens are never persisted**: not on disk, in logs, in a database, in error reports or in archive files. Held in memory for the life of one job only.
- No secrets in the repo. Secrets by env var name; `.env.example` holds names only. The developer's Harvest test token lives in `~/.harvest_test` outside the repo.
- Unit tests never hit the real Harvest API (fixtures or `respx`). Live tests use the `live` pytest marker and are skipped by default.
- Coverage ≥ 80% on product code. `ruff` clean.
- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`, `ci:`). Commit and push only after the tester's PASS.

## Commands
- Install: `uv sync`
- Test: `uv run pytest -q` (live: `uv run pytest -m live`)
- Lint: `uv run ruff check . && uv run ruff format --check .`

## Product facts verified on 2026-11-17 (trial account)
- `GET https://id.getharvest.com/api/v2/accounts` with the token returns the account ID(s); the user pastes only the token.
- REST data: `https://api.harvestapp.com/v2/{clients,projects,time_entries,invoices,estimates,expenses,...}` with headers `Authorization: Bearer`, `Harvest-Account-Id`, `User-Agent`.
- Receipt images: `expense.receipt.url` — GET with the Bearer token returns the original file; without it, a login page.
- Invoice PDFs: no API endpoint. `{company.base_uri}/client/invoices/{invoice.client_key}.pdf` returns the PDF (also for drafts) with no token.
