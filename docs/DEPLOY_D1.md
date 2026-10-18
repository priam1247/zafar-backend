# Running the backend on Cloudflare D1

The app keeps SQLAlchemy and its models. Only the database driver changes:
`d1_dbapi.py` sends each SQL statement to Cloudflare's D1 REST API.

## What D1 changes, and what it doesn't

- **Free and card-free.** D1 free limits: 5 GB total storage, 500 MB per
  database, 5M rows read and 100k rows written per day. Writes fail with an
  error once the daily limit is hit, until midnight UTC.
- **Backups are managed.** Time Travel gives 7 days of point-in-time restore
  on the free plan. Still keep an off-site export for anything important.
- **No interactive transactions.** Each statement commits on its own.
  `executemany()` runs as one atomic batch. `rollback()` cannot undo writes
  already sent, and logs a warning when that happens.
- **Every query is an HTTPS request** to Cloudflare. Expect more latency per
  request than a local database.
- **The API still needs a server.** D1 holds the data. Something must still
  run FastAPI. See "Where the API runs" below.

## 1. Create the database

In the Cloudflare dashboard: Workers & Pages → D1 → Create database, for example
`zafar`. Copy the **database UUID** and your **account ID**.

## 2. Create an API token

Cloudflare dashboard → My Profile → API Tokens → Create Token → custom token.
Give it **D1 Read** and **D1 Write** permission, scoped to your account. Keep the
token in the host's secret store. Never commit it.

## 3. Set the environment

```
DATABASE_URL=d1
CLOUDFLARE_ACCOUNT_ID=<account id>
D1_DATABASE_ID=<database uuid>
CLOUDFLARE_API_TOKEN=<token>
JWT_SECRET_KEY=<python -c "import secrets; print(secrets.token_hex(32))">
```

## 4. Copy existing data (only if you have accounts in Neon)

```
python scripts/migrate_database.py --source "postgresql://...?sslmode=require" --target d1
```

The script refuses to write into a non-empty target, and it checks row counts on
both sides afterwards. It only reads from the source.

## 5. Smoke test before cutting over

1. Start the app with the variables above, then open `/health/db`. It should
   return `{"status": "ok", "db": "reachable"}`.
2. Register a test account, verify it, log in, and download one file.
3. Check the Cloudflare dashboard for the database's row counts.

## Things I could not verify from the build environment

The build environment could not reach Cloudflare's API, so these need checking
on the first live run:

- **Parameter types.** Cloudflare's docs type `params` as strings. The driver
  sends native JSON types (ints, floats, strings, null). If D1 rejects any of
  them, the fix is to stringify in `_convert_param`.
- **PRAGMA statements.** SQLAlchemy's `inspect()` runs `PRAGMA table_info` on
  startup. D1 should allow it, but confirm it.
- **Error messages.** Constraint violations are recognised by the text
  `constraint failed`. Confirm this matches D1's messages, so duplicate emails
  come back as a clean error and not a 500.

The automated tests use a fake D1 backed by SQLite. They cover the driver and the
ORM, not the live endpoint.

## Where the API runs: Koyeb (Free, Frankfurt)

The Koyeb service `zafar-backend` runs on the Free plan in Frankfurt at
`https://bloody-brigitta-ahzafar-de9b1709.koyeb.app`. Koyeb builds the existing
`Dockerfile`, and `app.py` reads the `PORT` it sets.

1. **Point the service at the new code.** The service currently deploys from
   GitHub. Its source branch must contain `d1_dbapi.py` and the D1 changes.
   Either merge the branch to `main`, or change the service's branch to
   `arena/2cdfd4a3-zafar-backend`.
2. **Set the environment variables.** Replace the old Neon `DATABASE_URL` and
   keep the other variables (Drive, SMTP, download signing) as they are.
   ```
   DATABASE_URL=d1
   CLOUDFLARE_ACCOUNT_ID=cd7a370efb7d93a2cf8406b284cbf471
   D1_DATABASE_ID=a8dfc286-4f7b-4823-a241-b584db474748
   ```
   Put these in Koyeb **Secrets**, not plain variables:
   - `CLOUDFLARE_API_TOKEN`
   - `JWT_SECRET_KEY`
3. **Health check.** Set it to `/health`, which doesn't touch the database.
4. **Redeploy**, then open `/health/db`. It should return
   `{"status": "ok", "db": "reachable"}`.

The Cloudflare Worker at `cloudflare-worker/api-proxy/` has its `ORIGIN` set to
this Koyeb URL. Redeploy the Worker with `wrangler deploy` so the frontend
(`VITE_API_URL`) keeps working without changes.

Koyeb's Free plan may sleep an idle service, so the first request after a quiet
period can be slow. D1 itself adds no wake-up delay.
