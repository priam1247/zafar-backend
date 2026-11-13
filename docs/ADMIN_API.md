# Admin API implementation and deployment

The portal contract is in the frontend repository at `docs/admin-portal.md`.
This API is mounted at `/admin`; every route re-reads the user's role from the
`users` row. A role claim in a JWT is never used as authorization. `NULL` role
means ordinary student. The current static frontend handles an unavailable API
with its 404/501 gate and speaks the implemented routes described below.

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

If a hosted one-shot console cannot provide interactive input, use
`python -m scripts.make_owner --enroll --non-interactive editor@gmail.com`.
This mode skips the code-confirmation prompt, writes the new encrypted seed, and
prints it for setup in the authenticator app. Use it only from a trusted
provider console; re-run it to replace the seed if it was copied incorrectly.
It does not change the account's existing role.

`POST /auth/login` accepts an optional `totp_code` form field. Owners with a
configured authenticator must provide it; returned owner JWTs carry the MFA
proof and all later protected requests check that proof against the current DB
role. The current frontend (`zafar-frontend` main, merged PR #39) conditionally
reveals the code field when it receives `401 admin_totp_required`, then submits
the code with the password form or Google JSON request. An owner without an
enrolled authenticator receives `403 admin_mfa_unavailable` and is directed to
server-side enrollment. Ordinary students and editor/viewer admins keep the
existing password flow; they are not prompted for an authenticator code.

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
- Student submissions are available at `POST /requests` and `POST /reports`.
  They require the same signed-in student token; email is taken from the
  authenticated account, never the request body. Requests accept a supported
  section, title, and optional note. Reports accept a file ID, reason, and
  optional note; the file ID must exist in the live library and its name is
  resolved server-side. Submissions are rate-limited by account, not shared IP.
  The current frontend displays/resolves these queues but does not yet expose
  student intake forms. Example bodies are `{"section":"primary","title":"Standard 8 science","note":"Term two"}` and
  `{"file_id":"<catalogue-id>","reason":"broken download","note":"Optional details"}`.
- `GET/PATCH /admin/requests` and `/admin/reports` list and resolve the stored
  submissions and remain owner/editor routes. The queues contain only records
  actually submitted; no placeholder rows are created.
- `GET/PATCH /admin/settings`: persistent announcement, maintenance flag,
  featured IDs, and daily limits. Updated limits are read by both quota
  responses and download enforcement. `GET /site-config` exposes only the
  public display fields; the current student site consumes them for the
  announcement strip, maintenance screen, and featured badge.
- Owners can change account roles in the Users panel via
  `PATCH /admin/users/{user_id}` with `{"role":"owner"|"editor"|"viewer"|null}`.
  Owner assignment requires the target account to be active and have TOTP
  enrolled; conflict codes include `owner_mfa_not_enrolled`,
  `owner_account_suspended`, `last_owner`, and `self_role_change`.
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
