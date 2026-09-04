# Deployment Checklist — KataBump + Cloudflare Workers

## 0. Prerequisites
- KataBump allocation (IP:port), e.g. `147.135.213.72:20208`
- A Google Cloud service account JSON with Drive read access to `DRIVE_ROOT_FOLDER_ID`
- `npm install -g wrangler` (or `npx wrangler` per-command, no global install needed)
- A Cloudflare account (free tier is enough for both Workers)

## 1. Generate real secrets (do this before anything else)
```bash
python -c "import secrets; print('JWT_SECRET_KEY=' + secrets.token_hex(32))"
python -c "import secrets; print('DOWNLOAD_SIGNING_SECRET=' + secrets.token_hex(32))"
```
Save both — you'll need `DOWNLOAD_SIGNING_SECRET` in two places (backend env var AND the Worker secret) and it must be **identical** in both.

## 2. Backend — KataBump environment variables
Set these on KataBump (however its dashboard exposes env vars — check "Environment" or "Variables" in your allocation's settings):
```
JWT_SECRET_KEY=<from step 1>
DOWNLOAD_SIGNING_SECRET=<from step 1>
DOWNLOAD_PROXY_BASE_URL=https://zafar-dl.<you>.workers.dev   # fill in after step 4
DRIVE_ROOT_FOLDER_ID=<your Drive folder id>
SMTP_USER=<your gmail address>
SMTP_PASS=<16-char Gmail App Password — NOT your normal password>
```
Everything else has a working default in `config.py` (daily limits, TTLs) — only override what you need to change.

Get a Gmail App Password: Google Account → Security → 2-Step Verification (must be on) → App Passwords → generate one for "Mail".

Upload `service_account.json` to the same directory as `app.py` (or set `GOOGLE_SERVICE_ACCOUNT_FILE` to wherever you put it). **Never commit this file** — it's already in `.gitignore`.

## 3. Backend — install and run
```bash
pip install -r requirements.txt
pip install uvloop httptools   # optional, ~2x throughput per app.py's comment
python app.py
```
Test locally: `curl http://127.0.0.1:20208/health` → `{"status":"ok"}`

Database migrations run automatically on every startup (`main.py`'s `_run_migrations()`) — no manual Alembic step needed.

## 4. Deploy the download-proxy Worker (signed Drive downloads)
```bash
cd cloudflare-worker/download-proxy
npx wrangler deploy
npx wrangler secret put DL_SECRET
# paste the SAME value as DOWNLOAD_SIGNING_SECRET from step 1
```
Copy the resulting `https://zafar-dl.<you>.workers.dev` URL into the backend's `DOWNLOAD_PROXY_BASE_URL` (step 2) and restart the backend.

## 5. Deploy the API proxy Worker (HTTPS front door)
Your frontend will be HTTPS (Vercel/Netlify) but KataBump only serves plain HTTP — browsers silently block that (mixed content), so a second Worker fronts the API too.
```bash
cd cloudflare-worker/api-proxy
# edit api-proxy.js: set ORIGIN to your real KataBump IP:port if different
npx wrangler deploy
```
Copy the resulting `https://zafar-api.<you>.workers.dev` URL — this is your frontend's `API_BASE`.

## 6. Point the frontend at the proxy
```bash
# .env (Vite) or your host's env settings
VITE_API_URL=https://zafar-api.<you>.workers.dev
```
Rebuild and redeploy — env vars are baked in at build time; changing them without rebuilding does nothing.

## 7. Tighten CORS
Once you know your real frontend origin, edit `main.py`:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://your-frontend.vercel.app"],  # exact match, no trailing slash
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
```
Redeploy the backend after this change.

## 8. Keep KataBump awake
Free-tier containers can sleep/expire. Add a free uptime pinger (UptimeRobot, cron-job.org) hitting `https://zafar-api.<you>.workers.dev/health` every 5 minutes — this checks the same path your users take, so you're alerted if either the Worker or the backend breaks.

## 9. End-to-end test
1. `curl https://zafar-api.<you>.workers.dev/health` → `{"status":"ok"}`
2. Register a real Gmail address on the deployed frontend → check inbox for the 6-digit code
3. Verify → login → confirm DevTools Network tab shows calls to the `workers.dev` URL returning 200s
4. List papers → click download → confirm the URL is `https://zafar-dl.<you>.workers.dev/dl/...` and the file downloads with the correct filename
5. Download past your daily limit → confirm you get a 429, not a successful download

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| `Mixed Content` error in browser console | Frontend calling the raw KataBump `http://` URL instead of the `zafar-api` Worker |
| `net::ERR_CONNECTION_REFUSED` | KataBump container asleep/expired, or wrong port — test `/health` directly against the IP:port |
| CORS error | `allow_origins` doesn't exactly match your frontend's origin (scheme + host, no trailing slash) |
| 403 from Cloudflare instead of your API | `ORIGIN` in `api-proxy.js` has the wrong IP:port |
| Verification email never arrives | `SMTP_USER`/`SMTP_PASS` unset or wrong — check server logs, it prints the code to console as a fallback |
| Downloads work but aren't quota-limited | `DOWNLOAD_PROXY_BASE_URL` or `DOWNLOAD_SIGNING_SECRET` unset — falls back to permanent unsigned Drive links, see SECURITY.md |
