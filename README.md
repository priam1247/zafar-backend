# Zafar Backend

FastAPI backend for the Zafar learning-materials library. The production
frontend origin is **`https://studymw.netlibrary.dpdns.org`**. Configure that
exact origin in `ALLOWED_ORIGINS` and in the download Worker. See [the deployment
notes](docs/FRONTEND_DEPLOYMENT.md).

## Required deployment settings

The service intentionally fails during startup if any of these are blank:

- `JWT_SECRET_KEY`
- `DOWNLOAD_PROXY_BASE_URL`
- `DOWNLOAD_SIGNING_SECRET` (must match the Worker `DL_SECRET`)
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `SMTP_USER` and `SMTP_PASS`

`DRIVE_ROOT_FOLDER_ID` defaults to the established Zafar Materials (Sorted)
folder (`1SWv792HexEVx7tHaSBIZvgxKwgb6-lp4`); override it only if the library
root folder changes.

The optional Drime Cloud source can be enabled with `DRIME_API_KEY`; when
configured, its PDFs are combined with Google Drive by section. Primary School
is exposed separately as `section=primary` and shares the existing Papers
quota. The Drime API key must be stored privately in Koyeb and the download-
proxy Worker.

The only supported registration domain is `@gmail.com`: the current code
verification flow uses Gmail SMTP and the policy is validated at registration,
so the frontend should explain/check that before the student enters a password.
Do not broaden the policy until delivery to other providers has been tested.

Google Identity Services sign-in is available at `POST /auth/google`. It
requires the frontend OAuth **Web client ID** in the API's `GOOGLE_CLIENT_ID`
environment setting and the same ID in the frontend. The API verifies the
signed ID token's audience and verified Gmail address; it does not use or need
the OAuth client secret. With `GOOGLE_CLIENT_ID` unset, this endpoint returns
`google_not_configured` and does not accept sign-ins.

`ALLOWED_ORIGINS` is a comma-separated list of exact HTTPS origins, for example:

```text
ALLOWED_ORIGINS=https://studymw.netlibrary.dpdns.org
```

See [`.env.example`](.env.example) for the complete environment reference.

## API/frontend contract notes

- `GET /drive/papers?section=...` returns PDF items (50 by default, with
  `limit`/`offset` pagination); `X-Total-Count` is the filtered total across
  Google Drive and Drime before pagination. `GET /drive/categories?section=...`
  returns distinct sorted immediate-parent folder names for data-driven chips.
- A missing item in the cached Drive tree triggers one forced refresh before a 404.
- `POST /drive/refresh` is intentionally not exposed to ordinary users.
- Auth and Drive responses use `Cache-Control: no-store`. All API responses add
  basic browser security headers. API errors preserve their string `detail` and
  may provide `X-Error-Code`, `X-Attempts-Remaining`, and `Retry-After` headers.
- Access tokens contain `sub` (internal username), `uid`, `name`, and `email`.
- Login accepts email or a legacy username. Quotas are separate UTC calendar-day
  buckets: papers (10), books (3), notes (3), and marking keys (10), configurable
  with `DAILY_PAPER_LIMIT`, `DAILY_BOOK_LIMIT`, `DAILY_NOTES_LIMIT`, and
  `DAILY_MARKING_KEYS_LIMIT`. `/auth/quota` returns actual per-bucket usage.

## Admin API

The backend admin router, real-data metrics, role checks, audit log, persistent
settings, moderation queues, exports, and first-owner/TOTP bootstrap are described
in [the admin API deployment notes](docs/ADMIN_API.md). The frontend contract is
in `priam1247/zafar-frontend/docs/admin-portal.md`. Note that the browser login
form still needs an owner TOTP field before an owner can sign into the portal;
never bypass the backend's owner MFA check to work around that UI gap.

## Run tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```
