import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import inspect, text

from config import settings
from database import Base, engine
import models  # noqa: F401 — must be imported so create_all sees the tables
from routers import auth as auth_router
from routers import drive as drive_router

# Most PaaS deploys (Koyeb included) have no "upload a file" step the way
# KataBump's file manager does — so if the whole service_account.json
# content was pasted into an env var instead, write it out to the path
# Drive expects before anything tries to read it. KataBump-style deploys
# (a real service_account.json already sitting next to this file) are
# untouched — this only fires when the env var is actually set.
_sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
if _sa_json:
    with open(settings.google_service_account_file, "w") as _f:
        _f.write(_sa_json)

# Creates zafar.db and its tables on first run if they don't exist yet.
Base.metadata.create_all(bind=engine)


def _run_migrations():
    """
    create_all() above only creates tables that don't exist yet — it never
    alters an existing one. This app has no Alembic set up, so every
    hand-rolled schema change lives here, in one connection / one commit.
    Safe to run on every startup.
    """
    # inspect() works against SQLite, Postgres, or anything else SQLAlchemy
    # supports — unlike "PRAGMA table_info", which is SQLite-only and
    # throws a syntax error on Postgres.
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("users")}

    with engine.connect() as conn:
        if "email" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email)"))

        if "is_verified" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_verified BOOLEAN DEFAULT false"))
            conn.execute(text("UPDATE users SET is_verified = true"))  # grandfather old users
        if "verification_code" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN verification_code VARCHAR"))
        if "code_expires_at" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN code_expires_at DATETIME"))

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_downloads_user_time "
            "ON download_logs(user_id, downloaded_at)"
        ))
        conn.commit()  # one commit for everything


_run_migrations()

app = FastAPI(
    title="Zafar API",
    version="0.2.0",
    # Disable interactive docs in production if you like: docs_url=None, redoc_url=None
)

# Paper/book list JSON compresses ~85% — a real win on slow mobile connections.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# NOTE: allow_credentials=True with "*" is ignored by browsers per spec anyway.
# You use Bearer tokens (not cookies), so credentials aren't needed at all.
# Locked to the real frontend origin (zafarh.dpdns.org via GitHub Pages).
# The http:// entry is only here for the window before GitHub Pages
# finishes issuing its cert — once "Enforce HTTPS" is checked in the repo's
# Pages settings, delete the http:// line and keep https:// only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.library.zafarh.dpdns.org", "http://www.library.zafarh.dpdns.org"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth_router.router)
app.include_router(drive_router.router)


@app.get("/health")
def health():
    return {"status": "ok"}
