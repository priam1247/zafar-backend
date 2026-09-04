# Zafar Backend

FastAPI backend for a paper/book download platform: email-verified auth, JWT sessions, daily download quotas, and a Google Drive catalog served through Cloudflare Workers (HTTPS front door + quota-enforced signed downloads).

## Architecture
```
Browser (HTTPS frontend, e.g. Vercel/Netlify)
  ├─ API calls  -> CF Worker "zafar-api"  -> HTTP -> KataBump FastAPI backend
  └─ Downloads  -> CF Worker "zafar-dl"   -> Google Drive (HMAC-signed, quota-enforced)
```
The backend talks to SQLite (via SQLAlchemy) and the Google Drive API (via a service account, read-only). Two Cloudflare Workers exist because KataBump only serves plain HTTP: `zafar-api` gives the backend an HTTPS address (fixes mixed-content blocking), and `zafar-dl` re-serves Drive files with a correct filename/Content-Disposition and a short-lived signature so download links can't be shared or reused to bypass the quota.

## Project layout
```
config.py              Settings (env-var driven, with working defaults)
database.py            SQLAlchemy engine/session, SQLite WAL pragmas
models.py               User, DownloadLog
auth.py                 Password hashing, JWT issue/verify, signed-URL minting
drive_service.py        Google Drive wrapper with an in-memory TTL cache
email_utils.py           Gmail SMTP verification-code sender
main.py                 FastAPI app, CORS, gzip, startup migrations
app.py                  uvicorn entrypoint (KataBump)
schemas.py              Pydantic request/response models
routers/auth.py          /auth/*  — register, verify, resend, login, me, quota
routers/drive.py         /drive/* — list papers, request a signed download URL
cloudflare-worker/download-proxy/   Signed Drive download proxy (zafar-dl)
cloudflare-worker/api-proxy/        HTTPS front door for KataBump (zafar-api)
frontend-example/api-client.js     Reference fetch-based API client
tests/                  pytest suite (auth, quota, signing, caching)
```

## Setup (local dev)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional — defaults work out of the box
python app.py
```
Open `http://127.0.0.1:20208/health` → `{"status": "ok"}`. With no `SMTP_USER`/`SMTP_PASS` set, verification codes print to the console instead of emailing.

## Configuration
All settings are environment variables (see `.env.example` for the full list with explanations). Nothing is required for local dev — every setting has a working default. Before deploying with real users, set at minimum:

| Variable | Why |
|---|---|
| `JWT_SECRET_KEY` | The baked-in default is public — rotate it |
| `DRIVE_ROOT_FOLDER_ID` | Points at your actual Drive folder |
| `SMTP_USER`, `SMTP_PASS` | So verification emails actually send |
| `DOWNLOAD_PROXY_BASE_URL`, `DOWNLOAD_SIGNING_SECRET` | Without these, downloads silently fall back to permanent unsigned links — see SECURITY.md |

## Deployment
See [DEPLOYMENT.md](./DEPLOYMENT.md) for the full KataBump + Cloudflare Workers checklist, including wrangler commands and the mixed-content (HTTP backend / HTTPS frontend) fix.

## API endpoints
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/auth/register` | — | Gmail address + password; sends a 6-digit code |
| POST | `/auth/verify` | — | `{email, code}` |
| POST | `/auth/resend` | — | New code for an unverified account |
| POST | `/auth/login` | — | Form-encoded `email`+`password`, returns a JWT |
| GET | `/auth/me` | Bearer | Current user |
| GET | `/auth/quota` | Bearer | Today's paper/book download counts |
| GET | `/drive/health` | Bearer | Confirms the service account can reach Drive |
| POST | `/drive/refresh` | Bearer | Busts the Drive tree + section-ID caches |
| GET | `/drive/papers` | Bearer | `?section=papers\|books\|notes\|marking-keys`, `?category=`, `?q=` |
| POST | `/drive/download/{file_id}` | Bearer | Enforces quota, logs it, returns a signed 5-min URL |

Interactive docs at `/docs` (Swagger UI) once the server is running.

## Testing
```bash
pip install -r requirements-dev.txt
pytest
```
22 tests covering registration/verification/login/resend, the naive/aware datetime regression, quota enforcement (including the 429 path), signed-URL generation/verification, and the Drive cache's hit/miss/refresh behavior. No real Google or SMTP credentials are needed — Drive calls are monkeypatched in `tests/conftest.py`.

## Security
See [SECURITY.md](./SECURITY.md) — what's implemented, what this review found and fixed, and what's still open.

## Performance
See [PERFORMANCE.md](./PERFORMANCE.md) — caching layers, cold-start/memory tradeoffs, and query-efficiency notes.

## Troubleshooting
See the table at the bottom of [DEPLOYMENT.md](./DEPLOYMENT.md).
