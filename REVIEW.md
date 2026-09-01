# Code Review — Findings

Format: ISSUE → SEVERITY → FIX. Security issues are in [SECURITY.md](./SECURITY.md); this file covers correctness, conflicts, and anything else found while reconciling your baseline code against the upgrade doc.

## Conflicts between the upgrade doc and itself
The doc contains **two different versions of `routers/drive.py`** presented as complete files:
- Section 9, "pattern to merge into yours" — flat `category` query param (`paper`/`book`), no section awareness, calls `drive_service.find_cached_file`.
- Later, under "routers/drive.py (full upgraded file)" — section-aware (`papers`/`books`/`notes`/`marking-keys`), with its own `_section_id_cache`, and matches the `SECTION_FOLDERS` structure your existing `routers/drive.py` already uses.

→ SEVERITY: High (would have caused a broken merge — the two don't share a URL contract)
→ FIX: Implemented the later, section-aware version, since it's the one explicitly following the "Issues found, line by line" review and matches your actual Drive folder layout (`Past Papers`/`Books`/`Notes`/`Marking Keys`). Discarded section 9's draft.

## Correctness bugs (from the doc's own review, verified and applied)
- `/auth/verify` raised `TypeError` (surfacing as an unhandled 500) whenever a code had actually expired, because SQLite returns naive datetimes and the comparison used an aware one. → SEVERITY: High → FIX: `_expired()` normalizes with `.replace(tzinfo=timezone.utc)` before comparing. Covered by `tests/test_auth.py::test_verify_expired_code_does_not_500`.
- `func.lower(User.email) == identifier` in login defeats the unique index on `email`, forcing a full table scan on every login attempt once the table grows. → SEVERITY: Medium (perf, not correctness) → FIX: compare `User.email == identifier` directly, relying on emails always being stored lowercased at write time.

## Found beyond the doc
- **Registering an existing-but-unverified email hard-failed with a 400**, leaving a user stuck if they lost the original code and re-registered instead of using `/auth/resend`. → SEVERITY: Medium (UX/correctness) → FIX: doc's upgraded `register()` re-issues a fresh code for unverified accounts instead of rejecting them; kept.
- **`_generate_username` did one DB query per candidate suffix** (register `john`, `john2`, `john3`... each round-tripping the DB until a free one is found) — a pathological case with a common local-part could be slow. → SEVERITY: Low (perf) → FIX: one `LIKE` query fetches every taken variant, then the free suffix is picked in Python.
- **Quota check-then-insert race** in `POST /drive/download/{file_id}` — not present in the doc's discussion at all. Two near-simultaneous requests from the same user close to their limit can both pass the count check before either commits. → SEVERITY: Low for this deployment (single low-traffic app; documented rather than "fixed" — see SECURITY.md for the tradeoff and a proposed fix if you want it closed).
- **Silent insecure fallback**: `make_signed_download_url()` returns a permanent, unsigned Drive link if `DOWNLOAD_PROXY_BASE_URL`/`DOWNLOAD_SIGNING_SECRET` are unset — which quietly reopens the exact quota-bypass hole the redesign closes, with no warning at request time. → SEVERITY: Medium → Documented in SECURITY.md as a deploy-time checklist item rather than changed, since making it hard-fail would break local dev without those vars set — your call on whether to tighten this later.

## Missing pieces, completed
- `schemas.py` — the doc only gave the delta for `PaperOut` ("you didn't attach it"); wrote the complete file matching every router import, including a new `DownloadUrlOut` for the `/drive/download/{file_id}` response shape (id/expires_in/quota), and dropped the now-unused `DownloadLogIn`/old `/download-log` endpoint since the new drive.py doesn't reference them.
