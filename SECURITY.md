# Security Hardening Checklist

## Currently implemented (in this upgrade)
- Passwords hashed with bcrypt (`passlib`), never stored/logged in plaintext
- JWT auth (HS256), 7-day expiry, `Authorization: Bearer` — no cookies, so no CSRF surface
- Email verification via a 6-digit code, constant-time compared (`secrets.compare_digest`) to avoid timing side-channels
- Verification codes expire after 10 minutes, generated with `secrets.randbelow` (CSPRNG, not `random`)
- Gmail-only signups (reduces spam/throwaway-address abuse given SMTP delivery is Gmail-specific)
- Download URLs are short-lived (5 min default) and HMAC-signed (`SHA-256`) by the Worker — a copied/shared link stops working after expiry
- Quota is enforced server-side at the only endpoint that mints a download URL, and logged before the URL is returned
- CORS scoped to `GET`/`POST` and the two headers actually used (`Authorization`, `Content-Type`); `allow_credentials=False` since Bearer tokens don't need it
- `.gitignore` already excludes `service_account.json`, `.env`, and the SQLite DB file

## Found and fixed by this review
| Issue | Severity | Fix |
|---|---|---|
| A **live Google service-account private key** was present in the delivered zip (`service_account.json`) | **Critical** | Rotate it in Google Cloud Console immediately; never commit/zip this file. Not included in this output. |
| `/drive/papers` returned a permanent, unauthenticated-once-copied Drive URL — quota was only enforced if the client *chose* to call `/download-log` first | Critical | Removed `download_url` from the list response; only `POST /drive/download/{file_id}` mints a URL, and it enforces quota before doing so |
| `user.verification_code != payload.code` compared with `!=`, not constant-time | Medium | Switched to `secrets.compare_digest` |
| SQLite hands back naive datetimes; comparing against `datetime.now(timezone.utc)` (aware) raised `TypeError` → 500 on every expired-code check | High (correctness) | Normalize with `.replace(tzinfo=timezone.utc)` before comparing |
| Default `JWT_SECRET_KEY` is a real value baked into `config.py`, and was committed | Critical | Left the default in place (so the app still runs with zero setup) but flagged with a `SECURITY:` comment; **you must set a real env var and rotate before going live** — see DEPLOYMENT.md step 1 |

## Not yet addressed — recommended next
- **No rate limiting / brute-force protection** on `/auth/login` or `/auth/verify`. A 6-digit code has 1,000,000 combinations; with no attempt cap, an attacker with the target's email could script guesses within the 10-minute window. Cheapest fix on a single free-tier instance: an in-memory sliding-window limiter (e.g. `slowapi`) keyed on IP + email, capping `/auth/verify` to ~5 attempts per code and `/auth/login` to ~10/min per IP. A dict-based limiter works fine given `workers=1`.
- **JWTs can't be revoked.** A stolen token is valid for up to 7 days with no server-side kill switch. If that's a concern, either shorten `JWT_EXPIRE_MINUTES` or add a `token_version` column on `User` that's included in the JWT payload and checked on every request — bump it to invalidate all existing tokens for that user (e.g. on password change).
- **Quota check-then-insert is not atomic.** Two concurrent requests from the same user near their limit can both pass the count check before either commits, allowing one extra download. Low-impact for a free hobby project; if it matters, add a `threading.Lock` per user ID around the check-and-insert in `routers/drive.py`.
- **`make_signed_download_url` silently falls back to a permanent, unsigned Drive link** if `DOWNLOAD_PROXY_BASE_URL` or `DOWNLOAD_SIGNING_SECRET` is unset — this quietly reopens the quota-bypass hole this whole redesign exists to close. Worth adding a startup check that refuses to boot (or at least logs a loud warning) if these are unset once you're past local dev.
- **No input length/format cap on Drive `file_id`** in `/drive/download/{file_id}` beyond FastAPI's default path-param handling — low risk since it's checked against the cached file list before anything happens, but a malformed value still costs a cache lookup.
- **Secrets management**: right now everything lives in KataBump env vars / `.env`. That's fine for a single free-tier deployment; if you ever add a second environment (staging) or a collaborator, consider a `.env.production` kept out of any shared drive/chat, and rotating `JWT_SECRET_KEY` + `DOWNLOAD_SIGNING_SECRET` whenever anyone who had access leaves the project.
- **CORS is still `allow_origins=["*"]`** until you complete DEPLOYMENT.md step 7 — fine for local dev, not for production.

## Recommendation priority
1. Rotate the leaked service-account key (today)
2. Set real `JWT_SECRET_KEY` / `DOWNLOAD_SIGNING_SECRET` env vars before any real users sign up
3. Add basic rate limiting on `/auth/login` and `/auth/verify`
4. Tighten CORS once the frontend domain is fixed
5. Everything else in "not yet addressed" is nice-to-have for a single-developer, free-tier hobby project — not urgent
