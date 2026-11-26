# HANDOVER

## Status in one paragraph
Phases 0 and 1 are done. `exit-archive export --out DIR` reads `HARVEST_TOKEN` from the environment and writes a complete, checksummed zip from a Harvest account. The archive holds JSON + CSV for 14 resources plus invoice payments, receipts and invoice PDFs. On 2026-11-24 it passed its single user-approved live run against the trial account: counts matched, the receipt size and invoice PDF were verified, the manifest verified, and the token was absent from the zip. Every task went through the executor → tester → commit loop; T1.1 and T1.2 each needed one fix loop and every other task passed first time. Suite: 421 tests, 99.38 % coverage, ~100 s, ruff/mypy clean. Planned hours used: 12.5 of 28. Left: Phase 2, the offline `index.html` viewer (3 h); Phase 3, the web app (6.5 h); Phase 4, ship (6 h). 2026-11-25: Phase 2 done — T2.1 (viewer data bundle) and T2.2 (offline `index.html`, wired into the export) both passed first time; 513 tests, 99.44 %; e2e 16 passed in Chromium. T3.1 (job store, runner, cleanup, concurrent-safe redaction) passed after one fix loop; 607 tests. T3.2 (FastAPI web app: landing, token form, progress, download, privacy draft; payment seam fails closed) passed after one fix loop, which also fixed log redaction so it keeps `record.args` intact (uvicorn access logs work); 750 tests. Phase 3 done 2026-11-25: T3.3 (Stripe Checkout Session verification, respx mocks only, Stripe key as a redacted `Secret`) passed first time; 889 tests. T4.1+T4.2 done: Dockerfile (pinned digests, non-root uid 10001, `$PORT`) and GitHub Actions CI (lint, types, tests, reveal grep, docker build + `/healthz` smoke), green on main. T4.3 hardening passed two independent tester runs and is committed as f3dc0bc: security headers, a 16 KB body limit, a POST /jobs rate limit keyed via `TRUSTED_PROXY_HOPS`, an Origin check, our own path-only access log, `Bearer%20` scrubbing and `Secret.__getstate__` returning None. Suite: 1050 passed / 3 xfailed, 98.89 %. 2026-11-26: T4.3a (A1, job-id-shaped runs masked anywhere in logged paths) passed first time as c039351; T4.4 + T4.5.1 (`docs/DEPLOY.md`, `render.yaml`) committed as 5a8a243, CI green. Suite: 1125 passed / 2 xfailed, 98.89 %. Planned hours used: 26.75 of 28.75. Next: T4.5.1a (deployer, repo only) makes `/data` writable by uid 10001 even on a root-owned disk mount, proven in CI; meanwhile the main session asks the user the four T4.5.2 questions (below); the user's yes then starts T4.5.2 (Render service + disk) once T4.5.1a is green. The user decided D1 = Render and D2 = Stripe Payment Link; the user wants to be asked before every deployment step (accounts, keys, money).

## What exists
- `CLAUDE.md`, `docs/BRIEF.md`, `.claude/agents/*.md`, `.claude/commands/pipeline.md` — project rules, brief, agent roles, `/pipeline` loop
- `PLAN.md` (architecture, token security model, archive layout, decision log), `TODO.md` (tasks + acceptance), `HANDOVER.md` (this file)
- `pyproject.toml`, `uv.lock`, `.python-version` (3.12), `.env.example` (names only) — ruff, mypy strict, pytest with `-m 'not live'` and an 80 % coverage gate
- `src/exit_archive/secrets.py` — `HarvestToken` (slotted, immutable, masked repr/str/format, unpicklable, `reveal()`), `redact()`, `install_log_redaction(token|None) -> uninstall` over one process-wide, lock-guarded, refcounted registry (uninstall in any order; the last one restores the original factory only if ours is still current; malformed log calls never raise)
- `src/exit_archive/jobs/store.py` — `JobStore` (stdlib sqlite3 `$DATA_DIR/jobs.sqlite3`; no token or account column; statuses queued/running/done/failed/expired; partial unique index on `checkout_session_id` over non-failed rows → `SessionInUseError`)
- `src/exit_archive/jobs/runner.py` — `JobRunner(store, data_dir, max_concurrent=2, export=…)`: `submit(token, account_id, session_id) -> job_id`, `recover_on_startup()`, `wait_idle()`, `aclose()` (open jobs → failed/cancelled); exports into `$DATA_DIR/archives/<job_id>/`; stores only redacted `ExitArchiveError` text or a type name; nothing but `CancelledError` escapes a task; `ARCHIVE_TTL` 7 days
- `src/exit_archive/jobs/cleanup.py` — `sweep(store, data_dir, now)` (expire archives, orphan work dirs / `.zip.part` > 1 h, row-less dirs; never follows symlinks; re-reads status per entry), `run_cleanup_loop()`
- `src/exit_archive/errors.py` — `ExitArchiveError`, `SessionInUseError`, `AuthError` 401, `ForbiddenError` 403, `NotFoundError` 404, `RateLimitedError` 429, `HarvestUnavailableError` 5xx, `ExportError`; messages from safe fields only
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
- `src/exit_archive/web/` — `app.py` (`create_app(settings, export, payments)`, module-level `app`; routes `/`, `/start`, `POST /jobs`, `/jobs/{id}`, `/jobs/{id}/download`, `/healthz`, `/privacy`; custom 4xx/5xx handlers that never echo input; lifespan wires redaction, store, recovery, one sweep, cleanup loop), `settings.py` (`Settings.from_env`), `forms.py` (token ≤ 512, account id digits, session id), `payments.py` (`PaymentVerifier` protocol, `AllowAllVerifier`, `StripeVerifier` with format + mode checks before any call, 10 s timeout, no redirects/retries; `MalformedSessionError` → 400, `PaymentProviderError` → 503); `/start` verifies before showing the token form, `POST /jobs` re-verifies, `templates/`, `static/app.css` (no inline script/style)
- `src/exit_archive/web/security.py` — pure-ASGI middlewares, outermost first: SecurityHeaders (CSP `'self'`, HSTS, nosniff, DENY, no-referrer, Permissions-Policy; `no-store` on `/start`, `/jobs/*`, POSTs), then (after AccessLog and ServerError) BodyLimit (16 KB, chunked counted; non-numeric Content-Length → 400), OriginCheck (Origin/Referer vs `PUBLIC_BASE_URL` or Host; absent → allowed; `null` → 403), RateLimit (POST /jobs 5 per 10 min, LRU 10 k keys, `client_key()` = socket peer or the N-th XFF entry from the right)
- `src/exit_archive/web/accesslog.py` — `AccessLogMiddleware` logs `METHOD path status Nms` on `exit_archive.access` (no query, headers, IP; `mask_path` rewrites `/jobs/<id>…` and then replaces every `[A-Za-z0-9_-]{20,}` run anywhere with `<x>`, before %XX-encoding and the 120-char cap); `access_logging()` silences `uvicorn.access` for the lifespan
- `src/exit_archive/cli.py` — `exit-archive export --out DIR [--account-id ID]`:
  - exit codes: 0 ok, 2 usage, 3 auth, 4 other error, 130 interrupted
  - installs log redaction and pins httpx/httpcore to WARNING
  - never prints a traceback
- `Dockerfile`, `.dockerignore` — multi-stage uv build on `python:3.12-slim@sha256:…`, uv 0.12.11 pinned, non-root uid 10001 owning `/data`, `CMD sh -c "exec uvicorn … --port ${PORT:-8000} --workers 1 --proxy-headers"`
- `docs/DEPLOY.md` — env table (10 names), local docker run, Stripe key rotation, logs, rollback, expiry and disk sizing; Render section: Blueprint click steps (need the user's yes), D7 options, Stripe test steps 1-4, first-deploy checks (headers, redirect, `/healthz`, `/data` ownership, rate-limit keying)
- `render.yaml` — Render Blueprint: one Docker web service `exit-archive`, `starter`, 1 instance, `healthCheckPath: /healthz`, `autoDeployTrigger: checksPass`, disk `archives` 10 GB at `/data`, `PAYMENT_REQUIRED="true"`, `DATA_DIR=/data`, `MAX_CONCURRENT_JOBS="2"`, `TRUSTED_PROXY_HOPS="1"`, `STRIPE_SECRET_KEY` / `STRIPE_PAYMENT_LINK_URL` / `PUBLIC_BASE_URL` `sync: false`; region commented (default `oregon`)
- `.github/workflows/ci.yml` — on push to main and PRs: `test` job (ruff, format, mypy, pytest with the coverage gate, `.reveal()` allow-list over `*.py`) and `docker` job (build, `/healthz` smoke, non-root check); actions pinned by SHA; no secrets
- `tests/` — unit, respx and adversarial suites per module (`tests/harvest/`, `tests/archive/`, `tests/test_*.py`). `tests/live/test_live_export.py` is marked `live`; it uses the `harvest_live_credentials` fixture in `tests/conftest.py` and copies the zip to `./tmp/live/` only when `EXIT_ARCHIVE_KEEP_LIVE_ZIP=1`.

## How to run, test, deploy
- Install: `uv sync` (only when dependencies change; Python 3.12 via `uv python install 3.12` if missing)
- Test: `uv run --offline pytest -q -W error::pytest.PytestDeprecationWarning` (1125 passed / 2 xfailed at 5a8a243, coverage gate 80 %; `e2e` and `live` deselected by default)
- Lint/types: `uv run --offline ruff check . && uv run --offline ruff format --check . && uv run --offline mypy src`
- Token grep: `grep -rln "\.reveal()" src/` must list only `secrets.py` and `harvest/http.py` and `web/payments.py` (Stripe key header)
- Viewer e2e (optional): `uv sync --group e2e` once (network), `uv run --offline --group e2e playwright install chromium`, then `uv run --offline --group e2e pytest -m e2e -q`. Skips cleanly without Playwright/Chromium
- Live test (**only with the user's yes**): `uv run --offline pytest -m live -q`
- CLI (**only with the user's yes for the live token**): `set -a; . ~/.harvest_test; set +a; uv run --offline exit-archive export --out ./tmp/out`
- Web: `PAYMENT_REQUIRED=false DATA_DIR=./tmp/web uv run --offline uvicorn exit_archive.web.app:app --port 8765`, then open `http://localhost:8765/`
- CI: `curl -s "https://api.github.com/repos/Soham-1827/exit_archive/actions/runs?per_page=1"` → check `conclusion` (repo is public)
- Deploy: not yet. Steps and checks are in `docs/DEPLOY.md` (Render section); the Blueprint is `render.yaml`. Creating the Render service (T4.5.2) needs the user's yes and waits for T4.5.1a (CI green).
- Push: plain `git push` over HTTPS (`gh` is not installed)

## Decisions made
- CLI and core library before the web app: prove the complete export on the live account first; the web app reuses `export_account()`.
- Stack: Python 3.12, uv, httpx async, FastAPI + Jinja2 server-rendered. This is the brief's stack, with no frontend build.
- Jobs: in-process asyncio, single uvicorn worker, SQLite for job metadata only (no token column). Restarts fail running jobs.
- Archives live on local persistent disk and are deleted 7 days after finishing.
- Offline viewer: a static `index.html` plus a `data/viewer-data.js` script, because `file://` cannot `fetch()` JSON.
- Token handling:
  - memory only, in the redacted `HarvestToken` type
  - `.reveal()` only in `secrets.py`, `harvest/http.py` and `web/payments.py` (Stripe key)
  - sent only to https `*.harvestapp.com`
  - every export test asserts the token is absent from the zip and the logs
- PDF URLs (permanent `client_key`) and signed receipt redirect URLs count as secrets in logs, so httpx/httpcore are pinned to WARNING.
- A per-file failure (receipt, PDF, one invoice's payments) goes into `MANIFEST.missing[]`. Auth, rate-limit and outage errors abort the export.
- CSV formula-injection guard is on; the JSON keeps original values.
- 2026-11-24, user: the live token may be used for T1.7's single run, with a copy kept in `./tmp/live/`. Any further live use needs a new yes.
- Web hardening (T4.3): rate-limit client key from `TRUSTED_PROXY_HOPS` (N-th `X-Forwarded-For` entry from the right; never uvicorn `--forwarded-allow-ips='*'`); Origin check only when Origin/Referer is present; our own path-only access log, `uvicorn.access` silenced; no HEAD support (GET-only routes answer HEAD with 405; checks use GET).
- `render.yaml` fixes `PAYMENT_REQUIRED=true`: the repo config never ships a free mode. Deploys only after CI passes.
- A1 fix (T4.3a): any `[A-Za-z0-9_-]{20,}` run in a logged path is masked, rather than normalising paths, which is brittle.
- `TRUSTED_PROXY_HOPS=1` on Render: Render's docs do not describe its `X-Forwarded-For`; 1 (right-most) cannot be forged by a client and at worst over-limits; verified at T4.6.1a.
- `/data` ownership is pre-empted before the first paid deploy (T4.5.1a): a root entrypoint chowns only the data directory itself, then `setpriv` drops to uid 10001 with no capabilities before uvicorn starts. The app process never runs as root; CI proves it on a simulated root-owned mount.
- Full list with reasons: PLAN.md §6 and the decision log.

## Open decisions for the user
- **D1 Hosting — decided 2026-11-25: Render.** Blueprint `render.yaml` (T4.5), Starter service + 10 GB disk (~$10/mo). The user is installing the Render GitHub App; creating the service needs a separate yes.
- **D2 Payments — decided 2026-11-25: Stripe Payment Link at $49.** The code needs no account. The exact Stripe steps for the user (test mode first, at T4.6) are in TODO.md → Blocked / needs the user → D2.
- **T4.5.2 question set** (main session asks via AskUserQuestion, one call, four standalone questions; do not start the click-through until T4.5.1a is committed with CI green):
  1. Header `Render cost` — "Create the Render service from render.yaml now? It is one Starter web service plus a 10 GB disk, about $10 a month on your Render account." Options: `Yes, create it` (you click through the Blueprint steps in DEPLOY.md while I guide and check) · `Not yet` (nothing is created; we stop before any paid step).
  2. Header `Stripe first` — "The app will not start without Stripe values. How should the first deploy get them?" Options: `A: Stripe test mode first (Recommended)` (free, about 10 minutes: a test product, a $49 Payment Link and a read-only restricted key, pasted into Render when it asks; no real money moves) · `B: Short smoke test without payment` (you set PAYMENT_REQUIRED=false by hand in the Render dashboard, then switch it back and add the Stripe values; the site is free while it is off).
  3. Header `Region` — "Which Render region should the service run in? It cannot be changed after creation." Options: `Oregon, US West (Recommended)` (the default; no repo change) · `Ohio or Virginia, US East` (needs a one-line render.yaml commit first) · `Frankfurt, EU` (for mostly European buyers; needs a one-line render.yaml commit first). Singapore is available via Other.
  4. Header `GitHub App` — "Can the Render GitHub App see the exit_archive repository? Check GitHub → Settings → Applications → Render → Repository access." Options: `Yes, it has access` · `Not yet, or not sure` (I will walk you through granting access before anything else).
- **D7 Stripe test setup before the first deploy** (new, needed at T4.5.2): the app will not start with `PAYMENT_REQUIRED=true` and no Stripe values. Recommended (A): do D2 steps 1-4 in Stripe test mode (free) just before creating the Render service and paste the values when Render prompts; then fix the Payment Link redirect to the real host. (B): a short first deploy with `PAYMENT_REQUIRED=false` set by hand in the dashboard. Either way, the site is effectively free until live keys (test cards work), so keep the URL unannounced until T4.6 passes. Also: Render region (default `oregon`).
- **D6 Support contact** (new): the error and privacy pages have no support email yet. Ask the user for an address before launch; it becomes `SUPPORT_EMAIL`.
- **D3 Domain**: optional (~$12/yr).
- **D4 Tokens that see several accounts**: I recommend an optional Account ID field that fails with the account list when ambiguous. The CLI already behaves this way.
- **D5 Privacy page wording**: approve it before launch.
- **Estimate PDF live check**: the URL is unverified because the trial account has no estimates. To check it: add one estimate, then OK one more live run.
- **Trial token**: I recommend keeping it until T4.6 (the launch check runs a real export), then revoking it in Harvest → Developers. If you'd rather revoke it now, create a fresh one for T4.6.

## Next 3 tasks
1. **T4.5.1a `/data` writable by uid 10001 on a root-owned mount (deployer, repo only, 0.5 h).** Root entrypoint that chowns only the data dir, then `setpriv` to 10001 with no capabilities; CI proves PID 1 is uid 10001 with `CapEff` 0 and that a root-owned bind mount at `/data` still reaches `/healthz`. Main session gates, commits `ci: drop to uid 10001 after making the data dir writable on root-owned mounts`, pushes and checks CI.
2. **Ask the user the four T4.5.2 questions** (Open decisions above) in parallel with 1. Create nothing.
3. **T4.5.2 create the Render service + 10 GB disk (≈ $10/mo)** only after the user's yes and T4.5.1a green: the user clicks through DEPLOY.md's Blueprint steps (region commit first if not `oregon`; D7 path as chosen), the deployer runs the first-deploy checks. Then T4.6 launch check with Stripe test mode, D5 privacy wording and D6 support email.

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
  - The Docker daemon is down locally (still on 2026-11-25); the CI `docker` job is the build proof.
  - `gh` is not installed; check CI with `curl` on the GitHub API.
  - WSL on `/mnt/d` (NTFS) shows permission bits as 777, so don't rely on chmod in tests.
- **Code ordering**
  - `write_manifest` hashes whatever is in the work dir, so the viewer (T2.2) must be written before it.
  - `install_log_redaction()` stacks one hook per token. That is fine for the CLI; the web app needs T3.1.5.
  - FastAPI's default 422 response echoes the submitted input, so T3.2 must replace it.
  - The web lifespan (T3.2.3) must install redaction and the httpx/httpcore WARNING pin once.
- **Viewer e2e on this WSL box** needs `libnss3`, `libnspr4`, `libasound2`, which are not installed system-wide. The T2.2 executor unpacked them into a session scratchpad and used `LD_LIBRARY_PATH`, which does not persist. To run e2e locally again, either repeat that or ask the user to `sudo apt install libnss3 libnspr4 libasound2` (never run sudo without asking). CI would use `playwright install --with-deps chromium`.
- **Deploy (T4.5):** Render does not document who owns a mounted disk. T4.5.1a makes the image chown `/data` itself at start and then drop to uid 10001; still confirm at first deploy with `ls -ln /data` and a test job landing in `/data/archives`. A Render Shell may run as root, so judge ownership by `ls -ln`, not by whether the shell can write. Never fix it by running the app as root.
- **uvicorn `--proxy-headers` does nothing on Render** (it trusts only 127.0.0.1). That is fine: the app builds no absolute URLs, and the rate limiter reads `X-Forwarded-For` itself via `TRUSTED_PROXY_HOPS`. A wrong `PUBLIC_BASE_URL` (anything but the exact `https://<host>` origin) makes every POST 403; leaving it unset falls back to the Host header.
- **Two tester runs once overlapped on the same file** (T4.3). Run at most one tester per task at a time.
- **Unverified on real data:** the estimate PDF URL, rate limits on a large account, and pagination past page 1 (all under Later).
- **Tests**
  - `pytest -m live` alone switches the coverage gate off on purpose (it prints ~33 %); any other marker expression keeps the 80 % gate.
  - `tests/__init__.py` exists so tests can import from `tests.conftest` and `tests.test_exporter`.

## Secrets and where they live
- `HARVEST_TOKEN`, `HARVEST_ACCOUNT_ID`: the developer trial account, in `~/.harvest_test` outside the repo, mode 600.
- `STRIPE_SECRET_KEY` (restricted key) and `STRIPE_PAYMENT_LINK_URL`: not created yet. They will live in the hosting platform's environment settings.
- `PUBLIC_BASE_URL`, `DATA_DIR`, `MAX_CONCURRENT_JOBS`, `PAYMENT_REQUIRED`, `TRUSTED_PROXY_HOPS`: config, not secret; all listed by name in `.env.example`. On Render, `STRIPE_*` and `PUBLIC_BASE_URL` are `sync: false` (entered in the dashboard).

## Last updated
2026-11-26 (orchestrator review after T4.3a, T4.4 and T4.5.1). Last commit: 5a8a243 (DEPLOY.md + `render.yaml`, pushed; CI green). These doc edits (TODO.md, PLAN.md, HANDOVER.md) are uncommitted; the main session commits them with `docs: mark T4.3a, T4.4 and T4.5.1 done and pre-empt /data ownership`.
