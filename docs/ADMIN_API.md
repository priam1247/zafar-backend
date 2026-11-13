# Admin API implementation and deployment

The portal contract is in the frontend repository at `docs/admin-portal.md`.
This API is mounted at `/admin`; every route re-reads the user's role from the
`users` row. A role claim in a JWT is never used as authorization. `NULL` role
means ordinary student. The frontend's 404/501 gate remains useful during rollout.

## Safe bootstrap and owner MFA

The first owner is never selected by an HTTP request. Use an existing verified
account, from a trusted terminal connected to the production transactional DB:

```sh
python -m scripts.make_owner owner@gmail.com
```

The CLI prints an authenticator enrollment URI and secret once, then requires a
valid current TOTP before it writes the owner role. TOTP secrets are encrypted
with a key derived from `JWT_SECRET_KEY`, and accepted time steps cannot be
replayed. Keep `JWT_SECRET_KEY` stable to preserve the enrolled seed; rotating it
requires re-enrolling the owner's authenticator. To enroll another admin account
before assigning its role:

```sh
python -m scripts.make_owner --enroll editor@gmail.com
```

`POST /auth/login` accepts an optional `totp_code` form field. Owners with a
configured authenticator must provide it; returned owner JWTs carry the MFA
proof and all later protected requests check that proof against the current DB
role. The current static frontend login form does not yet collect this field, so
its owner sign-in UX needs a matching frontend change before the owner can use
`/admin` in a browser. Ordinary students and editor/viewer admins keep the
existing password flow. Google owner login likewise needs `totp_code` added to
the frontend JSON payload.

## Implemented route scope

- `GET /admin/whoami`: any admin role; `401` unauthenticated and `403` students.
- `GET /admin/overview?days=14`: real account/download counts, zero-filled UTC
daily buckets, top downloaded files, and logged zero-result searches.
  `top_subjects` is omitted because the current production data has no reliable
  subject field; the backend does not infer one from filenames.
- `GET /admin/users?q=&limit=25&offset=0`: owner-only search and pagination,
  with verified status, provider, suspension, last activity, and confirmed
  download counts. Mutations are owner-only and audited: suspend/restore,
  send password-reset code, role change, delete.
- Requests/reports list and status transitions are owner/editor routes. Their
  tables start empty and only represent rows actually written to the tables.
  Student submission forms/routes are not part of the current frontend/API.
- `GET/PATCH /admin/settings`: persistent announcement, maintenance flag,
  featured IDs, and daily limits. Updated limits are read by both quota
  responses and download enforcement. `GET /site-config` exposes only the
  public display fields for later student-site integration.
- `GET /admin/audit`, `GET /admin/ops`, and signed five-minute CSV exports.
  Operations reports DB kind, SQLite file size only when directly measurable,
  and recorded API 5xx counts. It omits storage quota/usage and backups because
  no trustworthy provider/backup inventory is configured in this service.

Every admin request is rate-limited by admin account, not shared IP. Mutations
write their audit record in the same SQL transaction on Postgres/SQLite. On D1,
admin writes use the D1 atomic batch API explicitly; ordinary SQLAlchemy session
commits are not assumed to be transactional there.

`/admin/*` responses are `no-store` and `X-Robots-Tag: noindex, nofollow`.
The static admin page itself remains unlinked from student pages and is also
blocked from indexing by its meta tag and `robots.txt`.
