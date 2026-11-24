# PLAN — Exit Archive MVP

Owner: orchestrator. Inputs: `docs/BRIEF.md`, `CLAUDE.md`. Budget: 14 days, ~28 h, < $500.

## 1. Goal
Token in, zip out. A Harvest user pastes one API token, pays $49, and gets a zip with every record (CSV + JSON), every receipt, every invoice/estimate PDF, an offline `index.html` viewer and a `MANIFEST.json`. The download link lives 7 days, then the files are deleted.

Build order: **core library + CLI first** (proves the export against the live trial account), then the offline viewer, then the web app, then deploy. The web app is a thin shell around the same `export_account()` call the CLI uses.

## 2. Architecture

Single Python 3.12 package `src/exit_archive/`, one process, one machine.

| Module | Responsibility | May depend on | Must not |
|---|---|---|---|
| `secrets.py` | `HarvestToken` wrapper (redacted `repr`/`str`), `redact()` helper, logging filter that scrubs the token value and any `Bearer …` string | stdlib | expose the raw value except via `.reveal()`, called only by `redact()` in this module and by `harvest/http.py` |
| `errors.py` | Typed errors (`AuthError`, `NotFoundError`, `RateLimitedError`, `HarvestUnavailableError`, `ExportError`) whose messages are built from safe fields only | stdlib | carry request headers or the token |
| `harvest/http.py` | Async httpx client: auth headers, `User-Agent`, rate limiter (100 req / 15 s), retry on 429 (`Retry-After`) and 5xx with backoff, error mapping, pagination via `links.next` | `secrets`, `errors`, httpx | log request headers |
| `harvest/accounts.py` | `GET id.getharvest.com/api/v2/accounts`, keep `product == "harvest"` accounts, pick one | `harvest/http` | — |
| `harvest/resources.py` | Registry of resources to export + feature-flag gating from `/v2/company`; async iterators per resource; invoice payments per invoice | `harvest/http` | write files |
| `harvest/files.py` | Binary downloads: receipts (Bearer, host allow-list), invoice/estimate PDFs (public `client_key` URL, **no token**), size cap, content-type check | `harvest/http` | send the token to any host outside the allow-list |
| `archive/writer.py` | JSON + CSV per resource (flattened dotted columns), safe file names, streaming writes to a work dir | stdlib | hold every resource in memory at once |
| `archive/manifest.py` | `MANIFEST.json`: counts per type, sha256 + bytes per file, `missing[]` with reasons, tool version, generated_at | stdlib | include the token or account owner email |
| `archive/zipper.py` | Zip the work dir, delete the work dir | stdlib | — |
| `viewer/` | `build_viewer(work_dir)`: writes `data/viewer-data.js` (slim projection) and `index.html` (inline CSS/JS, vanilla, no network) | stdlib, `importlib.resources` | load anything from the network; use `innerHTML` with record data |
| `exporter.py` | `export_account(token, out_dir, account_id=None, progress=cb) -> ExportResult` — orchestrates everything above | all of the above | know about HTTP requests from browsers |
| `cli.py` | `exit-archive export --out DIR [--account-id ID]`, token from `HARVEST_TOKEN` env var; `--version` | `exporter` | accept the token as a CLI argument (would land in shell history / `ps`) |
| `web/app.py` | FastAPI app, Jinja2 server-rendered pages: landing, start (after payment), token form, progress, download | `jobs`, `payments` | log request bodies; echo input in 422 errors |
| `web/security.py` | Security headers, body size limit, per-IP rate limit on job creation, Origin check on POST (owned by deployer) | starlette | — |
| `jobs/runner.py` | In-process asyncio job runner, semaphore (`MAX_CONCURRENT_JOBS`, default 2), progress state, token dropped when the job ends | `exporter`, `jobs/store` | persist the token |
| `jobs/store.py` | SQLite (stdlib `sqlite3`) job metadata: id, checkout_session_id, status, progress, error (safe text), created_at, expires_at, archive_path. **No token column.** | stdlib | — |
| `jobs/cleanup.py` | Hourly + on-startup sweep: delete archives past `expires_at`, mark rows `expired` | `jobs/store` | — |
| `payments.py` | Verify a Stripe Checkout Session (`GET /v1/checkout/sessions/{id}` via httpx, `STRIPE_SECRET_KEY`), paid + unused; `PAYMENT_REQUIRED=false` bypass for dev | httpx | store card or customer data beyond the session id |

## 3. Data flow (token → zip)
1. **Token in.** CLI: `HARVEST_TOKEN` env var. Web: HTTPS form POST body (never a query string). Wrapped immediately in `HarvestToken`.
2. **Account discovery.** `GET https://id.getharvest.com/api/v2/accounts` → keep `product == "harvest"`. One account → use it. Several and no `account_id` given → fail with a message listing account ids and names (not secrets).
3. **Company.** `GET /v2/company` → `base_uri`, `invoice_feature`, `expense_feature`, `estimate_feature`; disabled features are skipped and recorded in the manifest.
4. **Records.** For each resource in the registry, page through `links.next` under the rate limiter; each page is appended to `data/json/<resource>.json` (streamed) and `data/csv/<resource>.csv`. Resources: `clients, contacts, projects, tasks, users, user_assignments, task_assignments, time_entries, expense_categories, expenses, invoice_item_categories, invoices, invoice_payments (GET /v2/invoices/{id}/payments per invoice), estimate_item_categories, estimates`.
5. **Files.** Receipts: GET `expense.receipt.url` with the Bearer token, only if the URL host is on the allow-list (`*.harvestapp.com`); redirects to other hosts are followed **without** the `Authorization` header. Invoice PDFs: `{base_uri}/client/invoices/{client_key}.pdf` with no token. Estimate PDFs: try `{base_uri}/client/estimates/{client_key}.pdf` (unverified; T1.7 confirms). Concurrency 4. Each failure → `MANIFEST.missing[]`, never an aborted export.
6. **Viewer.** Build `data/viewer-data.js` (`window.EXIT_ARCHIVE_DATA = {...}`, slim fields only) + `index.html`. `file://` cannot `fetch()` JSON, which is why the data ships as a script.
7. **Manifest.** Walk the work dir, sha256 every file, write `MANIFEST.json` last (it does not list itself).
8. **Zip.** `exit-archive-<company-slug>-<YYYYMMDD>.zip`; delete the work dir.
9. **Token dropped.** The job/CLI releases its `HarvestToken` reference and closes the httpx client.
10. **Web only.** Archive served at `/jobs/{job_id}/download` (job_id = `secrets.token_urlsafe(32)`, a capability URL) until `expires_at = finished_at + 7 days`; the cleanup sweep deletes it.

## 4. Token security model
- **Memory only.** The token exists as a `HarvestToken` object inside one job (or one CLI run). Never written to disk, SQLite, logs, archive files, templates or error messages.
- **Redacted by type.** `repr()`/`str()`/f-strings of `HarvestToken` give `HarvestToken(***)`. Only `secrets.redact()` (to scrub) and `harvest/http.py` (to build the header) call `.reveal()`.
- **Scrubbed logs.** A logging filter installed at startup replaces the token value and any `Bearer \S+` with `***`. httpx/httpcore loggers stay at WARNING. No `show_locals` tracebacks, no third-party error reporting in the MVP.
- **Safe exceptions.** Harvest errors become typed errors built only from status code, resource name and a fixed message. The job runner catches everything, stores `type(exc).__name__` + the safe message, and never stores `str()` of an arbitrary exception without passing it through `redact()`.
- **Web input.** Token only in a POST body; `autocomplete="off"`; never re-rendered into a page; custom 422 handler that never echoes `input`; uvicorn access log has paths only (token never in a path); Origin check on POST.
- **Token stays on Harvest hosts.** Bearer token sent only to `id.getharvest.com`, `api.harvestapp.com` and receipt URLs on `*.harvestapp.com`; never to PDF URLs or redirect targets off the allow-list.
- **Verified by tests.** Every export test asserts the token string is absent from all archive bytes and from captured log output (`caplog` + `capsys`).
- **Developer test token** lives in `~/.harvest_test` (`HARVEST_TOKEN=`, `HARVEST_ACCOUNT_ID=`), read only by the `live` test helper; never copied into the repo or a prompt.

## 5. Archive layout
```
exit-archive-<company-slug>-<YYYYMMDD>/
├── index.html                  offline viewer (open by double-click; works from file://)
├── README.txt                  what is in here, how to verify with MANIFEST.json
├── MANIFEST.json               counts per type, sha256 + bytes per file, missing[], generated_at, tool version
├── data/
│   ├── json/<resource>.json    full Harvest objects, one array per resource
│   ├── csv/<resource>.csv      flattened (client.id, client.name, …), UTF-8 with BOM for Excel
│   └── viewer-data.js          slim projection for index.html
├── invoices/<number>_<id>.pdf
├── estimates/<number>_<id>.pdf
└── receipts/<expense_id>_<sanitized original file name>
```
File names come from Harvest data, so `archive/writer.py` sanitizes them: allow `[A-Za-z0-9._-]`, cap length at 100, prefix with the numeric id so names are unique, and assert the resolved path stays inside the work dir.

## 6. Key decisions
| # | Decision | Reason |
|---|---|---|
| K1 | Python 3.12 + uv + httpx (async) | Brief's stack; async gives cheap concurrency for PDFs/receipts. |
| K2 | Core library + CLI before any web code | Proves the hard part (complete export) against the live account first; the web app reuses `export_account()`. |
| K3 | FastAPI + Jinja2 server-rendered pages, no JS framework | Four pages; no build step; small attack surface. |
| K4 | In-process asyncio job runner, single uvicorn worker | No Redis/Celery to pay for or operate; volume is low. Cost: a deploy/restart fails running jobs (marked failed, buyer re-submits on the same paid session). |
| K5 | SQLite (stdlib) for job metadata only | Download links and "session already used" must survive restarts; SQLite needs no service. Holds no token. |
| K6 | Local persistent disk for archives, not S3 | One machine; disk is cheap on Render/Fly; S3 adds keys and code. Revisit if we scale out (Later). |
| K7 | Offline viewer = static `index.html` + `viewer-data.js` script, vanilla JS, `textContent` only | `file://` blocks `fetch()`; no network; no XSS from customer notes. |
| K8 | Rate limiter 100 req / 15 s (Harvest's documented limit) + honor `Retry-After` | Avoids 429 storms on 15-year accounts; limit not yet verified on a large account. |
| K9 | Per-resource streaming writes; viewer gets a slim projection | Keeps memory low enough for a 512 MB instance on large accounts. |
| K10 | Stripe Payment Link + server-side Checkout Session check | No checkout code to build; the session check stops people skipping payment; one job per session. |
| K11 | Token from env var in the CLI, never a flag | Flags leak into shell history and `ps`. |
| K12 | Missing files (PDF/receipt) recorded in `MANIFEST.missing[]`, export continues | One broken receipt must not cost the buyer the whole archive. |

## 7. Open decisions for the user
1. **D1 Hosting (blocks T4.5).** Recommendation: **Render**, one Starter web service (~$7/mo) + 10 GB persistent disk (~$2.50/mo) ≈ $10/mo, well inside budget. Why: automatic HTTPS, persistent disk for 7-day archives, a paid service never sleeps so in-process background jobs keep running, deploys straight from the GitHub repo via `render.yaml` with no CLI to learn. Caveats: 512 MB RAM (upgrade to Standard if the large-account test needs it), a service with a disk runs as one instance (fine, K4). Alternative: **Fly.io**, 1 GB shared-cpu machine + 10 GB volume ≈ $7/mo, more RAM per dollar, but needs `flyctl` and `min_machines_running = 1` / auto-stop off so jobs are not killed. Not recommended: serverless (Vercel, Lambda: no long jobs, no disk), Render free tier (sleeps, no disk), a bare VPS (cheapest, but you own TLS, patching and restarts). Check current prices before choosing.
2. **D2 Payments (blocks T3.3 live keys and T4.6).** Recommendation: **Stripe Payment Link** at $49, success URL `https://<host>/start?session_id={CHECKOUT_SESSION_ID}`, plus a restricted API key with read access to Checkout Sessions. You need a Stripe account (free; fees per charge). Alternative: Lemon Squeezy / Paddle as merchant of record if you do not want to handle sales tax/VAT for overseas buyers (higher fee, different verification code).
3. **D3 Domain.** Optional for launch (the Render/Fly subdomain works); ~$12/yr if you want one.
4. **D4 Multi-account tokens.** Recommendation: the web form has an optional "Account ID" field; if the token sees several Harvest accounts and none is given, the job fails with the list of account ids/names. $49 buys one account.
5. **D5 Privacy / data-handling page wording.** We will draft it (no token stored, archive deleted after 7 days, no analytics); you approve the wording before launch.

## 8. Risks and how the plan covers them
- Harvest changes the public invoice link or receipt URL → each file failure is recorded, not fatal (K12); live smoke test T1.7 is rerun before launch (T4.6).
- Large accounts: rate limits and memory → K8, K9; full large-account test is in "Later" because the trial account is small.
- Estimate PDF URL unverified → T1.5 tries the analogous URL; T1.7 records the result; if it fails, estimates ship as data only and the gap is documented.
- Token leak → section 4, enforced by tests in every export task and by tester's secret scan.

## 9. Decision log
- 2026-11-24 — Initial plan (orchestrator, plan mode). CLI-first ordering; Render recommended for hosting pending D1; Stripe Payment Link pending D2. Developer test-token file confirmed to contain the keys `HARVEST_TOKEN` and `HARVEST_ACCOUNT_ID` (names checked, values not read).
- 2026-11-24 — T1.1 review: `.reveal()` is permitted in exactly two places, `secrets.redact()` (it must know the value to scrub it) and `harvest/http.py` (to build the Authorization header). Any other caller is a review FAIL.
- 2026-11-24 — T1.1 review: `install_log_redaction()` chains record factories per token; with overlapping web jobs, an out-of-order uninstall would drop another job's redaction. Fine for the single-token CLI; T3.1.5 replaces it with one process-wide redactor over a set of active tokens.
- 2026-11-24 — T1.1 tester FAIL #1: the redaction hook made malformed log calls raise in the caller; must fall back instead. Also ruled: `HarvestToken` becomes a `__slots__` class (no `__dict__`, so `vars`/`asdict` cannot dump it), and fixed-status errors drop the `status_code` override (only `HarvestUnavailableError` keeps it, for 5xx).
- 2026-11-24 — T1.2 review: HTTP 500 is retried like 502/503/504 (all our calls are idempotent GETs, and completeness beats speed). Applied in T1.3.5 rather than a separate loop. Also accepted the executor's extra guard: `get()`/`paginate()` refuse absolute URLs on a different host, so a hostile `links.next` never receives the Bearer token.
- 2026-11-24 — T1.2 tester FAIL #1: host guard compared hostname only, so `http://` downgrades and other ports on the same host received the token. Rule: a followed URL must match the base URL's full origin (scheme, host, effective port). `links.next` cycles raise `ExportError`. 3xx gets its own clear error. The 500 retry (was T1.3.5) moves into this fix loop because it touches the same code.
- 2026-11-24 — T1.2 re-gate: `https://api.harvestapp.com:0443/` parses to port 443 and is treated as the same origin. Accepted: it is the same endpoint, so the token goes nowhere new.
- 2026-11-24 — T1.3 review: `invoice_payments` is listed in `skipped` when `invoice_feature` is off (payments belong to invoices; the manifest should say why they are absent). Payments are interleaved right after their invoice in `iter_all()` to stay streaming; T1.4 therefore writes one lazily opened stream per resource and builds each CSV in a second pass over a temp JSONL (column set varies per record).
- 2026-11-24 — T1.3 gate hardening notes, all accepted: (a) a 404/403 on one invoice's payments is recorded in `missing[]` and the export continues (a deleted invoice must not cost the buyer the archive; K12) — T1.6.1a; auth, rate-limit and outage errors still abort; (b) invoice ids < 1 rejected — T1.6.1a; (c) `company.base_uri` must be https on a dot-anchored `*.harvestapp.com` host before any PDF URL is built, since it comes from a response — T1.5.2a. T1.6 estimate 1 h → 1.5 h; plan total now 28 h.
