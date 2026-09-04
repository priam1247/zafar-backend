# Performance Notes

## Caching strategy (as shipped in this upgrade)
- **Drive folder-tree cache** (`drive_service._tree_cache`): the recursive walk under a section folder is cached in-process for `DRIVE_CACHE_TTL_SECONDS` (default 600s), keyed by folder ID. A cold `/drive/papers` request costs `1 + N` Drive API calls (N = subfolders); warm requests cost 0 and return in ~1ms. A `threading.Lock` ensures only one request rebuilds the cache on a miss — concurrent requests during a rebuild wait and then read the fresh result, instead of all independently hitting the Drive API.
- **Section-folder-ID cache** (`routers/drive._section_id_cache`): resolving `"papers"` → its Drive folder ID is itself one Drive API call (`find_child_folder`); cached for 1 hour since folder IDs essentially never change.
- **Cloudflare edge cache**: the download-proxy Worker sets `cf: { cacheEverything: true, cacheTtl: 86400 }` on its fetch to Drive — a popular paper downloaded repeatedly is served from Cloudflare's edge, not re-fetched from Drive or proxied through KataBump at all.
- **lru_cache on `get_drive_service()`**: the Google API client + credentials are built once per process, not per request. The Google client libraries are also imported lazily (inside the function, not at module load) — saves ~30-50MB RSS until the first actual Drive call, which matters on a memory-capped free tier.

## Cold starts
- The heaviest imports (`google-api-python-client`, `google-auth`) are deferred to first use rather than paid at boot — startup is faster and idle memory lower if a container spins up but no one hits `/drive/*` right away.
- `app.py` runs with `workers=1`: an extra uvicorn worker duplicates the whole process's RAM (including the loaded Google client) for very little throughput gain on typical free-tier CPU allocations — not worth it here.
- Consider `pip install uvloop httptools` (already noted in `app.py`'s comment) — uvicorn auto-detects and uses them, roughly 2x throughput for negligible extra memory.

## Memory
- `lazy="noload"` on `User.downloads` prevents SQLAlchemy from ever pulling a user's entire download history into memory via the ORM relationship — the quota queries use `COUNT`/`GROUP BY` instead, which SQLite answers from the index without materializing rows.
- The Drive tree cache holds parsed JSON (dicts), not raw API responses — for a few hundred papers this is low-single-digit MB, not a concern until the corpus grows into the tens of thousands of files, at which point consider capping cached fields to just `id`/`name`/`mimeType`/`size`/`category` (already the case) or moving to a persisted index.

## Database query efficiency
- `ix_downloads_user_time` composite index on `(user_id, downloaded_at)` keeps the quota `COUNT ... WHERE user_id = ? AND downloaded_at >= ?` index-backed regardless of table size — without it, this degrades to a full scan as `download_logs` grows.
- Quota is computed with one `GROUP BY category` query instead of two separate `COUNT` queries (`_count_today_by_category`) — halves round-trips on every `/auth/quota` and `/drive/download/{id}` call.
- Login compares `User.email == identifier` directly (not `func.lower(User.email) == identifier`) since emails are always stored lowercased at write time — wrapping a column in a function prevents SQLite from using the unique index on it, forcing a full table scan on every login attempt.
- `_generate_username` fetches every username matching the prefix in one query and picks a free suffix in Python, instead of one query per candidate suffix (the original did up to N round-trips for a common local-part like "john").

## Concurrent request handling
- `limit_concurrency=50` in `app.py` sheds load past that point (returns 503) instead of letting the process pile up requests until it OOMs — appropriate for a free-tier CPU/RAM cap where degrading gracefully beats crashing.
- `timeout_keep_alive=15` avoids holding idle keep-alive connections open indefinitely, freeing up the limited connection pool faster under load.
- SQLite in WAL mode (`PRAGMA journal_mode=WAL`) lets reads proceed without blocking on a concurrent writer — meaningful once you have more than a handful of simultaneous users, since the default rollback-journal mode serializes all access.
- `send_code()` (Gmail SMTP, 1-3s) now runs via `BackgroundTasks` instead of inline in the request — `/auth/register` and `/auth/resend` return in ~50ms instead of blocking a worker thread for the SMTP handshake.

## What's not optimized (and why that's probably fine here)
- The quota check-then-insert isn't wrapped in a stricter transaction (see SECURITY.md) — adding one costs complexity for a race that's unlikely to matter at this traffic scale.
- No response caching layer (e.g. Redis) in front of `/drive/papers` — the in-process cache already gets you to ~1ms warm, and adding an external cache service isn't justified until you're running more than one backend process, which `workers=1` deliberately avoids on this hosting tier.
