# HANDOVER

## Status in one paragraph
Phases 0 and 1 are done. `exit-archive export --out DIR` reads `HARVEST_TOKEN` from the environment and writes a complete, checksummed zip from a Harvest account. The archive holds JSON + CSV for 14 resources plus invoice payments, receipts and invoice PDFs. On 2026-11-24 it passed its single user-approved live run against the trial account: counts matched, the receipt size and invoice PDF were verified, the manifest verified, and the token was absent from the zip. Every task went through the executor → tester → commit loop; T1.1 and T1.2 each needed one fix loop and every other task passed first time. Suite: 421 tests, 99.38 % coverage, ~100 s, ruff/mypy clean. Planned hours used: 12.5 of 28. Left: Phase 2, the offline `index.html` viewer (3 h); Phase 3, the web app (6.5 h); Phase 4, ship (6 h). Hosting (D1) and payments (D2) still need the user's decision, but nothing before T4.5 depends on them.

## What exists
- `CLAUDE.md`, `docs/BRIEF.md`, `.claude/agents/*.md`, `.claude/commands/pipeline.md` — project rules, brief, agent roles, `/pipeline` loop
- `PLAN.md` (architecture, token security model, archive layout, decision log), `TODO.md` (tasks + acceptance), `HANDOVER.md` (this file)
- `pyproject.toml`, `uv.lock`, `.python-version` (3.12), `.env.example` (names only) — ruff, mypy strict, pytest with `-m 'not live'` and an 80 % coverage gate
- `src/exit_archive/secrets.py` — `HarvestToken` (slotted, immutable, masked repr/str/format, unpicklable, `reveal()`), `redact()`, `install_log_redaction()` (record-factory hook; malformed log calls fall back instead of raising)
- `src/exit_archive/errors.py` — `ExitArchiveError`, `AuthError` 401, `ForbiddenError` 403, `NotFoundError` 404, `RateLimitedError` 429, `HarvestUnavailableError` 5xx, `ExportError`; messages from safe fields only
- `src/exit_archive/harvest/http.py` — `HarvestClient`:
  - rate limiting: `RateLimiter`, 100 requests per 15 s
  - retries on 429 (`Retry-After`), 500/502/503/504 and transport errors; 3xx → error
  - same-origin guard on API URLs; `paginate()` with a loop guard and `MAX_PAGES`
  - `download()`: a separate header-less client; the token is added per hop only for https `*.harvestapp.com`; redirects followed by hand (≤ 5); writes `.part` then renames; 50 MB cap
  - `is_harvest_host_url()`, `DownloadError`
- `src/exit_archive/harvest/accounts.py` — `discover_accounts()` (keeps only `product == "harvest"`), `choose_account()`
- `src/exit_archive/harvest/resources.py` — `RESOURCES` (14), `fetch_company()` → `Company`, `skipped_resources()`, `iter_all(client, company, on_missing=)`, which streams `(name, record)` with payments right after their invoice. A 404/403 on payments goes to `missing[]`; invoice ids < 1 are rejected.
- `src/exit_archive/harvest/files.py` — `download_files(...)` → `FilesResult(files, missing)`. Receipts are fetched with the token, PDFs without. Validates `base_uri`; Semaphore(4).
- `src/exit_archive/archive/writer.py` — `ArchiveWriter`:
  - first pass writes JSON plus a temp JSONL; the second pass writes the CSV (UTF-8 BOM, formula-injection guard)
  - `safe_name()`, `resolve_inside()`, `flatten()`
- `src/exit_archive/archive/manifest.py` — `write_manifest(...)`: counts, sha256 + bytes per file, `missing[]`, `skipped[]`
- `src/exit_archive/archive/zipper.py` — `zip_archive(...)`: refuses symlinks, writes `.zip.part` then renames, removes the work dir
- `src/exit_archive/exporter.py` — `export_account(token, out_dir, account_id=None, progress=None) -> ExportResult`:
  - stages: accounts → company → records → files → archive
  - keeps only slim projections in memory
  - on any failure, the work dir is removed and no zip is left
- `src/exit_archive/cli.py` — `exit-archive export --out DIR [--account-id ID]`:
  - exit codes: 0 ok, 2 usage, 3 auth, 4 other error, 130 interrupted
  - installs log redaction and pins httpx/httpcore to WARNING
  - never prints a traceback
- `tests/` — unit, respx and adversarial suites per module (`tests/harvest/`, `tests/archive/`, `tests/test_*.py`). `tests/live/test_live_export.py` is marked `live`; it uses the `harvest_live_credentials` fixture in `tests/conftest.py` and copies the zip to `./tmp/live/` only when `EXIT_ARCHIVE_KEEP_LIVE_ZIP=1`.

## How to run, test, deploy
- Install: `uv sync` (only when dependencies change; Python 3.12 via `uv python install 3.12` if missing)
- Test: `uv run --offline pytest -q -W error::pytest.PytestDeprecationWarning` (421 tests, ~100 s, coverage gate 80 %)
- Lint/types: `uv run --offline ruff check . && uv run --offline ruff format --check . && uv run --offline mypy src`
- Token grep: `grep -rln "\.reveal()" src/` must list only `secrets.py` and `harvest/http.py`
- Live test (**only with the user's yes**): `uv run --offline pytest -m live -q`
- CLI (**only with the user's yes for the live token**): `set -a; . ~/.harvest_test; set +a; uv run --offline exit-archive export --out ./tmp/out`
- Web (after T3.2): `PAYMENT_REQUIRED=false DATA_DIR=./tmp uv run --offline uvicorn exit_archive.web.app:app`
- Deploy: not yet. `docs/DEPLOY.md` arrives with T4.4/T4.5.
- Push: plain `git push` over HTTPS (`gh` is not installed)

## Decisions made
- CLI and core library before the web app: prove the complete export on the live account first; the web app reuses `export_account()`.
- Stack: Python 3.12, uv, httpx async, FastAPI + Jinja2 server-rendered. This is the brief's stack, with no frontend build.
- Jobs: in-process asyncio, single uvicorn worker, SQLite for job metadata only (no token column). Restarts fail running jobs.
- Archives live on local persistent disk and are deleted 7 days after finishing.
- Offline viewer: a static `index.html` plus a `data/viewer-data.js` script, because `file://` cannot `fetch()` JSON.
- Token handling:
  - memory only, in the redacted `HarvestToken` type
  - `.reveal()` only in `secrets.redact()` and `harvest/http.py`
  - sent only to https `*.harvestapp.com`
  - every export test asserts the token is absent from the zip and the logs
- PDF URLs (permanent `client_key`) and signed receipt redirect URLs count as secrets in logs, so httpx/httpcore are pinned to WARNING.
- A per-file failure (receipt, PDF, one invoice's payments) goes into `MANIFEST.missing[]`. Auth, rate-limit and outage errors abort the export.
- CSV formula-injection guard is on; the JSON keeps original values.
- 2026-11-24, user: the live token may be used for T1.7's single run, with a copy kept in `./tmp/live/`. Any further live use needs a new yes.
- Full list with reasons: PLAN.md §6 and the decision log.

## Open decisions for the user
- **D1 Hosting**: I recommend Render, one Starter service plus a 10 GB disk (~$10/mo). Alternative: Fly.io (~$7/mo; needs flyctl and auto-stop off). Blocks T4.5.
- **D2 Payments**: I recommend a Stripe Payment Link at $49 with success URL `https://<host>/start?session_id={CHECKOUT_SESSION_ID}` and a restricted key that can read Checkout Sessions. You need a Stripe account. Alternative: Lemon Squeezy or Paddle as merchant of record, for sales tax/VAT.
- **D3 Domain**: optional (~$12/yr).
- **D4 Tokens that see several accounts**: I recommend an optional Account ID field that fails with the account list when ambiguous. The CLI already behaves this way.
- **D5 Privacy page wording**: approve it before launch.
- **Estimate PDF live check**: the URL is unverified because the trial account has no estimates. To check it: add one estimate, then OK one more live run.
- **Trial token**: I recommend keeping it until T4.6 (the launch check runs a real export), then revoking it in Harvest → Developers. If you'd rather revoke it now, create a fresh one for T4.6.

## Next 3 tasks
1. **T2.1 Viewer data bundle (executor):** writes `data/viewer-data.js` with slim fields. The dispatch must tell the executor that the tester tests it may change are none.
2. **T2.2 Offline `index.html` viewer (executor):** vanilla JS and `textContent` only. Its CSP must still work from `file://`; prove it with a Playwright e2e under the `e2e` marker. It also wires `build_viewer()` into `export_account()` before `write_manifest`.
3. **T3.1 Job store, runner and expiry cleanup (executor):**
   - SQLite with no token column
   - Semaphore of 2 concurrent jobs
   - T3.1.5: one process-wide redactor holding the set of active tokens
   - the cleanup sweep also removes orphan work dirs

## Gotchas
- **Agent rules**
  - Executors have twice edited tester-owned tests (both disclosed, both approved). From now on, each executor dispatch names the tester tests it may change; touching any other one means stop and report.
  - Testers must never run `-m live`, read `~/.harvest_test`, or open `tmp/live/*.zip`.
  - Subagents can't spawn subagents; the main session runs each dispatch.
- **Live token**
  - `~/.harvest_test` is `KEY=VALUE` with `HARVEST_TOKEN` and `HARVEST_ACCOUNT_ID`. Only the `live` fixture reads it. Never print it, copy it into the repo, or paste it into a prompt.
  - The live run needs the user's yes every time.
  - The kept zip with trial data is `tmp/live/exit-archive-personal-20261124.zip`, gitignored. Never stage `tmp/`.
- **Environment**
  - Use `uv run --offline …`; plain `uv run` stalled over 120 s re-resolving dependencies.
  - The Docker daemon was down when checked, so the deployer must say if `docker build` can't be verified.
  - `gh` is not installed; check CI with `curl` on the GitHub API.
  - WSL on `/mnt/d` (NTFS) shows permission bits as 777, so don't rely on chmod in tests.
- **Code ordering**
  - `write_manifest` hashes whatever is in the work dir, so the viewer (T2.2) must be written before it.
  - `install_log_redaction()` stacks one hook per token. That is fine for the CLI; the web app needs T3.1.5.
  - FastAPI's default 422 response echoes the submitted input, so T3.2 must replace it.
  - The web lifespan (T3.2.3) must install redaction and the httpx/httpcore WARNING pin once.
- **Unverified on real data:** the estimate PDF URL, rate limits on a large account, and pagination past page 1 (all under Later).
- **Tests**
  - `pytest -m live` alone switches the coverage gate off on purpose (it prints ~33 %); any other marker expression keeps the 80 % gate.
  - `tests/__init__.py` exists so tests can import from `tests.conftest` and `tests.test_exporter`.

## Secrets and where they live
- `HARVEST_TOKEN`, `HARVEST_ACCOUNT_ID`: the developer trial account, in `~/.harvest_test` outside the repo, mode 600.
- `STRIPE_SECRET_KEY` (restricted key) and `STRIPE_PAYMENT_LINK_URL`: not created yet. They will live in the hosting platform's environment settings.
- `PUBLIC_BASE_URL`, `DATA_DIR`, `MAX_CONCURRENT_JOBS`, `PAYMENT_REQUIRED`: config, not secret. T3.2 adds the names to `.env.example`.

## Last updated
2026-11-24. Last commit: 3c6d2a6 (feat: add export_account pipeline and exit-archive export CLI). T1.7 and this handover are about to be committed together.
