# HANDOVER

## Status in one paragraph
Phases 0 and 1 are done. `exit-archive export --out DIR` reads `HARVEST_TOKEN` from the environment and writes a complete, checksummed zip from a Harvest account. The archive holds JSON + CSV for 14 resources plus invoice payments, receipts and invoice PDFs. On 2026-11-24 it passed its single user-approved live run against the trial account: counts matched, the receipt size and invoice PDF were verified, the manifest verified, and the token was absent from the zip. Every task went through the executor → tester → commit loop; T1.1 and T1.2 each needed one fix loop and every other task passed first time. Suite: 421 tests, 99.38 % coverage, ~100 s, ruff/mypy clean. Planned hours used: 12.5 of 28. Left: Phase 2, the offline `index.html` viewer (3 h); Phase 3, the web app (6.5 h); Phase 4, ship (6 h). 2026-11-25: Phase 2 done — T2.1 (viewer data bundle) and T2.2 (offline `index.html`, wired into the export) both passed first time; 513 tests, 99.44 %; e2e 16 passed in Chromium. Planned hours used: 15.5 of 28. T3.1 in progress. The user decided D1 = Render and D2 = Stripe Payment Link; the user wants to be asked before every deployment step (accounts, keys, money).

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
- `src/exit_archive/archive/paths.py` — `receipt_file_name()`, `pdf_file_name()`, `RECEIPT_DIR`, `MAX_NUMBER_LEN`: one place for downloaded-file names (used by `harvest/files.py` and the viewer)
- `src/exit_archive/viewer/bundle.py` — `build_viewer_data(work_dir, company_name=None)` → `data/viewer-data.js` (`window.EXIT_ARCHIVE_DATA = {...};`, flat slim schema in the module docstring, `<>&` U+2028/9 escaped, paths only for files that exist); reads JSON via `archive.writer.iter_json_records()` one record at a time. Not yet wired into the exporter (T2.2.3)
- `src/exit_archive/viewer/__init__.py` + `viewer/index.html` — `build_viewer(work_dir, company_name)` writes `index.html` + `data/viewer-data.js`; called in `_export_into()` after `writer.close()`, before `write_manifest()`. Vanilla JS, `textContent` only, CSP by sha256 hash (+ `'self' file:` for the data script), hrefs only for `^(receipts|invoices|estimates)/[A-Za-z0-9._-]+$`
- `src/exit_archive/cli.py` — `exit-archive export --out DIR [--account-id ID]`:
  - exit codes: 0 ok, 2 usage, 3 auth, 4 other error, 130 interrupted
  - installs log redaction and pins httpx/httpcore to WARNING
  - never prints a traceback
- `tests/` — unit, respx and adversarial suites per module (`tests/harvest/`, `tests/archive/`, `tests/test_*.py`). `tests/live/test_live_export.py` is marked `live`; it uses the `harvest_live_credentials` fixture in `tests/conftest.py` and copies the zip to `./tmp/live/` only when `EXIT_ARCHIVE_KEEP_LIVE_ZIP=1`.

## How to run, test, deploy
- Install: `uv sync` (only when dependencies change; Python 3.12 via `uv python install 3.12` if missing)
- Test: `uv run --offline pytest -q -W error::pytest.PytestDeprecationWarning` (513 tests, coverage gate 80 %; `e2e` and `live` deselected by default)
- Lint/types: `uv run --offline ruff check . && uv run --offline ruff format --check . && uv run --offline mypy src`
- Token grep: `grep -rln "\.reveal()" src/` must list only `secrets.py` and `harvest/http.py`
- Viewer e2e (optional): `uv sync --group e2e` once (network), `uv run --offline --group e2e playwright install chromium`, then `uv run --offline --group e2e pytest -m e2e -q`. Skips cleanly without Playwright/Chromium
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
- **D1 Hosting — decided 2026-11-25: Render.** Blueprint `render.yaml` (T4.5), Starter service + 10 GB disk (~$10/mo). The user is installing the Render GitHub App; creating the service needs a separate yes.
- **D2 Payments — decided 2026-11-25: Stripe Payment Link at $49.** Still to ask the user, step by step: Stripe account, product + Payment Link (success URL needs the Render host), a restricted test-mode key, and later live keys.
- **D3 Domain**: optional (~$12/yr).
- **D4 Tokens that see several accounts**: I recommend an optional Account ID field that fails with the account list when ambiguous. The CLI already behaves this way.
- **D5 Privacy page wording**: approve it before launch.
- **Estimate PDF live check**: the URL is unverified because the trial account has no estimates. To check it: add one estimate, then OK one more live run.
- **Trial token**: I recommend keeping it until T4.6 (the launch check runs a real export), then revoking it in Harvest → Developers. If you'd rather revoke it now, create a fresh one for T4.6.

## Next 3 tasks
1. **T3.1 Job store, runner and expiry cleanup (executor, dispatched 2026-11-25):** stdlib only; no token column; partial unique index on session id; semaphore of 2; process-wide redactor (T3.1.5); sweep incl. orphan work dirs under `$DATA_DIR/archives/`.
2. **T3.2 Web routes and pages (executor):** starts with T3.2.0, adding fastapi/uvicorn/jinja2/python-multipart (network once). Custom 422 handler; lifespan installs redaction + httpx WARNING pin once.
3. **T3.3 Stripe gate (executor):** respx only; no Stripe account needed yet. Then Phase 4 (T4.1–T4.4 need no user input; T4.5/T4.6 need the user's yes at each step).

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
- **Viewer e2e on this WSL box** needs `libnss3`, `libnspr4`, `libasound2`, which are not installed system-wide. The T2.2 executor unpacked them into a session scratchpad and used `LD_LIBRARY_PATH`, which does not persist. To run e2e locally again, either repeat that or ask the user to `sudo apt install libnss3 libnspr4 libasound2` (never run sudo without asking). CI would use `playwright install --with-deps chromium`.
- **Unverified on real data:** the estimate PDF URL, rate limits on a large account, and pagination past page 1 (all under Later).
- **Tests**
  - `pytest -m live` alone switches the coverage gate off on purpose (it prints ~33 %); any other marker expression keeps the 80 % gate.
  - `tests/__init__.py` exists so tests can import from `tests.conftest` and `tests.test_exporter`.

## Secrets and where they live
- `HARVEST_TOKEN`, `HARVEST_ACCOUNT_ID`: the developer trial account, in `~/.harvest_test` outside the repo, mode 600.
- `STRIPE_SECRET_KEY` (restricted key) and `STRIPE_PAYMENT_LINK_URL`: not created yet. They will live in the hosting platform's environment settings.
- `PUBLIC_BASE_URL`, `DATA_DIR`, `MAX_CONCURRENT_JOBS`, `PAYMENT_REQUIRED`: config, not secret. T3.2 adds the names to `.env.example`.

## Last updated
2026-11-25. Last commit: dab901f (T2.1). T2.2 + these doc edits are about to be committed together; T3.1 dispatched next.
