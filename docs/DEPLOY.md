# Deploying Exit Archive

Exit Archive is one container: a FastAPI app plus an in-process job runner, with archives on a local disk mounted at `/data`. It runs as a single instance (one uvicorn worker). This file has a platform-neutral part and a [Render](#render) part.

No customer Harvest token is ever stored. Nothing in this guide needs one on the server.

## Environment variables

| Name | Purpose | Required | Default | Secret |
|---|---|---|---|---|
| `DATA_DIR` | Directory for archives and job records. The image sets it to `/data`. | Required (the image sets it) | `/data` in the image; none otherwise | no |
| `MAX_CONCURRENT_JOBS` | Archive jobs that may run at once. Whole number 1 to 8. | Optional | `2` | no |
| `PAYMENT_REQUIRED` | `true` or `false`, exactly. `true` makes `/start` require a paid Stripe Checkout session. Unset means `true`. Use `false` only for local runs. | Optional | `true` | no |
| `STRIPE_SECRET_KEY` | Restricted key (`rk_...`) with only Checkout Sessions: Read. A publishable `pk_` key is rejected. | Required when `PAYMENT_REQUIRED=true` | none | **yes** |
| `STRIPE_PAYMENT_LINK_URL` | The Stripe Payment Link buyers are sent to. Must be `https`. | Required when `PAYMENT_REQUIRED=true` | none | no |
| `PUBLIC_BASE_URL` | The site's own origin, used by the POST Origin check. See below. | Optional | unset (the request `Host` header is used) | no |
| `TRUSTED_PROXY_HOPS` | Number of proxies in front of the app whose `X-Forwarded-For` entry is trusted, counted from the right. Whole number 0 to 3. See below. | Optional | `0` (the socket peer) | no |
| `PORT` | Port uvicorn listens on. Read by the Docker `CMD`, not by the app. Hosts such as Render inject it. | Optional | `8000` | no |
| `HARVEST_TOKEN` | The CLI's Harvest token. **CLI only, never set on the server.** | CLI only | none | **yes** |
| `HARVEST_ACCOUNT_ID` | The CLI's Harvest account id. **CLI only, never set on the server.** | CLI only | none | no |

The app refuses to start, with an error naming the variable (never its value), when a required value is missing or malformed. `.env.example` lists the same names with blank values; never commit real values.

### `PUBLIC_BASE_URL`

Set it to exactly `https://<host>`: scheme and host, no path, no trailing slash, no port unless the site really uses one. Or leave it unset. When it is set, every POST must carry an `Origin` (or `Referer`) equal to it. A mismatch (`http://` instead of `https://`, a `www.` host, the wrong subdomain) makes every POST answer 403. When it is unset, the app compares the `Origin` to the request's own `Host`. If buyers get 403 on the token form, check this variable first.

### `TRUSTED_PROXY_HOPS`

The per-client rate limit on `POST /jobs` (5 per 10 minutes) needs a client address. The app takes the `TRUSTED_PROXY_HOPS`-th entry of `X-Forwarded-For` counting from the right, because the right-most entries are appended by your own proxies and the left-most entries are written by the client.

- Too low (for example `0` behind a proxy): the app sees the proxy's address, so every buyer shares one bucket and the whole site is limited to 5 jobs per 10 minutes.
- Too high: the app reads an entry the client wrote, so a client can forge `X-Forwarded-For` and escape the limit.
- `0` means "use the socket peer" and is correct only with no proxy in front.

Verify the value after every platform or proxy change (see the rate-limit check under Render).

## Run the container locally

No Stripe values are needed with `PAYMENT_REQUIRED=false`. The container starts as root only so its entrypoint can fix the data directory's owner, then runs the app as uid 10001 with no capabilities.

```bash
docker build -t exit-archive .
mkdir -p /tmp/ea-data
docker run --rm -p 8000:8000 \
  -e PAYMENT_REQUIRED=false \
  -e PORT=8000 \
  -v /tmp/ea-data:/data \
  exit-archive
```

Open `http://localhost:8000/`. `curl http://localhost:8000/healthz` returns 200. At start the entrypoint changes the owner of the bind-mounted top-level directory to uid 10001 (so `/tmp/ea-data` on the host becomes owned by 10001; no `chmod` needed, contents are not touched). A named volume works the same way: `-v ea-data:/data`. Starting with `--user 10001:10001` skips the entrypoint's chown, so the mounted directory must already be writable by that uid.

## Rotate `STRIPE_SECRET_KEY`

1. In Stripe (Developers, API keys), create a new restricted key with only **Checkout Sessions: Read**; every other permission None.
2. Set the new value as `STRIPE_SECRET_KEY` in the Render dashboard (service, Environment). Type or paste it there only; never in chat, the repo or a shell history.
3. Redeploy (saving an environment change on Render redeploys; otherwise Manual Deploy, Deploy latest commit).
4. Check a test purchase still reaches `/start`.
5. Revoke the old key in Stripe.

Rotate immediately if a key may have been exposed.

## Read the logs

On Render: service, Logs. The app writes one line per request through the `exit_archive.access` logger:

```
GET /healthz 200 3ms
POST /jobs 303 41ms
GET /jobs/<id>/download 200 120ms
```

Format: `METHOD path status duration`. Never logged: Harvest tokens, query strings (so no Stripe `session_id`), request or response bodies and headers, client IP addresses, and job ids (masked as `<id>`; any long id-like run in any path is masked). The uvicorn access log is silenced. The host's own request logs, if the platform keeps them, are outside the app's control; check them once after the first run.

## Roll back

Render: service, **Deploys**, pick the last good deploy, **Rollback**, then **Rollback to this deploy** (reuses that build, so it is fast). Rolling back in the dashboard turns auto-deploys off; after fixing the problem, re-enable them in Settings. Roll back only to a deploy whose build artifact is still retained. Archives on `/data` are untouched by a rollback. Docs: https://render.com/docs/rollbacks

Anywhere else: redeploy the previous image tag or commit.

## Expiry and disk sizing

Archives expire 7 days after the job finishes. The app sweeps once at startup and then every hour, deleting expired archives and orphaned job directories; a failed sweep is logged by exception type and retried an hour later.

Disk sizing for the 10 GB disk: archives live on the disk for up to 7 days plus up to one hour until the next sweep, so peak use is roughly (jobs per week) times (average archive size). At 10 GB, plan for no more than a few dozen medium archives in a week, and watch the disk usage graph in Render. Disks can grow but never shrink. Render keeps daily snapshots of a disk for 7 days.

## Render

Platform notes (from Render's docs): a service with a persistent disk runs as one instance and cannot do zero-downtime deploys, so each deploy has a short gap; only files under `/data` survive a deploy.

### Create the Blueprint (needs the user's yes, about $10/month)

This step creates paid resources: a Starter web service (about $7/month) and a 10 GB disk (about $2.50/month). Do it only after an explicit yes. Check current prices on Render's pricing page first.

Before you start, read "D7: Stripe values first" below.

1. Make sure the Render GitHub App is installed on this repository (GitHub, Settings, Applications, Render, Repository access).
2. In the Render dashboard click **New**, then **Blueprint**.
3. Select this repository and the `main` branch. Render reads `render.yaml` and shows one web service, `exit-archive`, with a 10 GB disk.
4. When prompted for the `sync: false` variables, enter:
   - `STRIPE_SECRET_KEY`: the Stripe **test** restricted key (`rk_test_...`).
   - `STRIPE_PAYMENT_LINK_URL`: the Stripe **test** Payment Link URL.
   - `PUBLIC_BASE_URL`: leave empty for now (the host is not known yet), or enter `https://exit-archive.onrender.com` if the dashboard shows exactly that name is free. Set it once the real host is known, as exactly `https://<host>`.
5. Confirm the region (the default is `oregon`; to change it, uncomment `region:` in `render.yaml` and commit before this step, since it cannot change later) and click **Apply** / **Deploy Blueprint**.
6. Note the service URL (`https://<name>.onrender.com`). It is `<host>` below.

Render ignores `sync: false` variables on later Blueprint updates, so to change those values use the service's Environment tab.

Deploys: `autoDeployTrigger: checksPass` deploys a commit only after its CI checks pass (the field is in Render's Blueprint spec: https://render.com/docs/blueprint-spec). If a deploy is ever needed without it, use Manual Deploy in the dashboard.

### D7: Stripe values first

The Blueprint fixes `PAYMENT_REQUIRED=true`, and the app does not start without `STRIPE_SECRET_KEY` and `STRIPE_PAYMENT_LINK_URL`. Two options:

- **A (recommended):** before the first deploy, do Stripe test-mode steps 1 to 4 below (free, no real money), and paste the key and Payment Link URL when Render prompts. Then point the Payment Link redirect at the real host.
- **B:** deploy first, setting `PAYMENT_REQUIRED=false` by hand in the Render dashboard for a short smoke test, then switch it back to `true` and set the Stripe values. Never put `false` in the repo.

Stripe test-mode steps (stay in test mode; one step at a time):

1. Create or sign in to a Stripe account (free).
2. Create a product "Exit Archive", one-time price $49 USD.
3. Create a Payment Link for it. After payment, redirect to `https://<host>/start?session_id={CHECKOUT_SESSION_ID}` (exact text including the braces; this needs the real host, so set it after the first deploy gives you `<host>`).
4. Create a restricted key with only Checkout Sessions: Read, everything else None. It starts with `rk_test_`.

Until live keys are set, test cards work, so the site is effectively free: keep the URL unannounced until launch checks pass.

### First-deploy checks

Replace `<host>` with the service host. Use GET; `HEAD` answers 405 on these routes.

1. Headers and status:
   ```bash
   curl -sS -o /dev/null -D - https://<host>/
   ```
   Expect `HTTP/2 200` and every security header added in T4.3: Content-Security-Policy, Strict-Transport-Security, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy.
2. HTTP to HTTPS redirect:
   ```bash
   curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' http://<host>/
   ```
   Expect `301` or `308` and a `https://<host>/` URL.
3. Health: `curl -sS -o /dev/null -w '%{http_code}\n' https://<host>/healthz` returns `200`.
4. **`/data` ownership.** Render mounts the persistent disk over `/data`, and its docs do not say who owns the mounted disk. The fix is applied in the image: `docker/entrypoint.sh` starts as root, creates `/data` if needed, changes the owner of that one directory (never its contents) to uid 10001 when it is not already, then drops to uid/gid 10001 with no capabilities (`setpriv`) before `exec uvicorn`. The app itself never runs as root. Still confirm it at first deploy:
   - In the service's Shell run `ls -ln /data`. The `.` entry (`ls -lnd /data`) and `archives` must show owner and group `10001`. A Render Shell may run as root, so judge by `ls -ln`, not by whether the shell can write.
   - Run a test job (test-mode purchase, trial token) and confirm the archive lands under `/data/archives` (`ls -ln /data/archives`).
   - If `/data` is not owned by 10001, or the deploy log shows `entrypoint: cannot chown data directory`, stop and report; do not work around it by running the app as root.
5. **Rate limit and forged `X-Forwarded-For` (T4.6.1a).** `render.yaml` sets `TRUSTED_PROXY_HOPS=1`. Render's public documentation I could read does not state how its router writes `X-Forwarded-For`, so `1` (the right-most entry, which a client cannot forge) is used; at worst it keys on a proxy hop and over-limits, which fails safe. Verify on the live host, using throwaway input so no real token is sent:
   - Send 6 `POST /jobs` requests from one machine. Expect the 6th to return 429.
   - From a second network (for example a phone hotspot) a request is still accepted (not 429). If it is 429, every buyer shares one bucket: the value is too low.
   - After the 5th request, send a 6th from the same machine with an added `-H 'X-Forwarded-For: 198.51.100.7'`. It must still be 429. If it succeeds, a client can forge the key: the value is too high.
   Adjust `TRUSTED_PROXY_HOPS` (Environment tab) and repeat until all three hold. Each POST also needs an `Origin` equal to `https://<host>` when `PUBLIC_BASE_URL` is set.

After step 3 of the Stripe steps, set `PUBLIC_BASE_URL` to exactly `https://<host>` and redeploy, then confirm a browser POST works (no 403).
