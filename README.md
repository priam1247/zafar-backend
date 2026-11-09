# Zafar Backend

FastAPI backend for the Zafar learning-materials library. The deployed browser
frontend's canonical HTTPS origin is **`https://library.zafarh.dpdns.org`** (with
an **h** in `zafarh`). Configure that exact value in `ALLOWED_ORIGINS` and in
the download Worker as `ALLOWED_ORIGIN`. A frontend hostname change is a
coordinated deployment step; see [the deployment notes](docs/FRONTEND_DEPLOYMENT.md).

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

The only supported registration domain is `@gmail.com`: the current code
verification flow uses Gmail SMTP and the policy is validated at registration,
so the frontend should explain/check that before the student enters a password.
Do not broaden the policy until delivery to other providers has been tested.

`ALLOWED_ORIGINS` is a comma-separated list of exact HTTPS origins, for example:

```text
ALLOWED_ORIGINS=https://library.zafarh.dpdns.org,https://preview.example.org
```

See [`.env.example`](.env.example) for the complete environment reference.

## API/frontend contract notes

- `GET /drive/papers?section=...` returns PDF items; `GET /drive/categories?section=...`
  returns the distinct sorted immediate-parent folder names for data-driven chips.
- A missing item in the cached Drive tree triggers one forced refresh before a 404.
- `POST /drive/refresh` is intentionally not exposed to ordinary users.
- Auth and Drive responses use `Cache-Control: no-store`. API errors preserve their
  string `detail` and may provide `X-Error-Code`, `X-Attempts-Remaining`, and
  `Retry-After` headers.
- Access tokens contain `sub` (internal username), `uid`, `name`, and `email`.
- Login accepts email or a legacy username. Quotas are UTC calendar-day buckets;
  notes and marking keys use the papers bucket.

## Run tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```
