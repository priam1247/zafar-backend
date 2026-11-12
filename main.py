from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from database import Base, engine, get_db
import models  # noqa: F401 — must be imported so create_all sees the tables
from routers import auth as auth_router
from routers import drive as drive_router
from config import settings


def _run_migrations():
    """
    create_all() only creates tables that don't exist yet — it never alters
    an existing one. This app has no Alembic set up, so every hand-rolled
    schema change lives here, in one connection / one commit. Safe to run
    on every startup.
    """
    # inspect() works against SQLite, Postgres, or anything else SQLAlchemy
    # supports — unlike "PRAGMA table_info", which is SQLite-only and
    # throws a syntax error on Postgres.
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("users")}
    dl_cols = {c["name"] for c in inspector.get_columns("download_logs")}

    with engine.connect() as conn:
        if "email" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR"))
        if "password_changed_at" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN password_changed_at TIMESTAMP"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email)"))

        if "is_verified" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_verified BOOLEAN DEFAULT false"))
            conn.execute(text("UPDATE users SET is_verified = true"))  # grandfather old users
        if "verification_code" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN verification_code VARCHAR"))
        if "code_expires_at" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN code_expires_at TIMESTAMP"))
        if "name" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN name VARCHAR"))
        if "verification_attempts" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN verification_attempts INTEGER DEFAULT 0"
            ))
            conn.execute(text("UPDATE users SET verification_attempts = 0"))
        if "reset_code" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN reset_code VARCHAR"))
        if "reset_code_expires_at" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN reset_code_expires_at TIMESTAMP"))
        if "reset_attempts" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN reset_attempts INTEGER DEFAULT 0"
            ))
            conn.execute(text("UPDATE users SET reset_attempts = 0"))

        if "confirmed" not in dl_cols:
            conn.execute(text(
                "ALTER TABLE download_logs ADD COLUMN confirmed BOOLEAN DEFAULT false"
            ))
            # Grandfather every pre-existing row as confirmed — there's no
            # way to know in hindsight whether an old download actually
            # completed, and treating them all as unconfirmed would let
            # every existing user's quota silently reset for free.
            conn.execute(text("UPDATE download_logs SET confirmed = true"))

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_downloads_user_time "
            "ON download_logs(user_id, downloaded_at)"
        ))
        conn.commit()  # one commit for everything


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Moved out of module scope: importing main.py (e.g. in tests, or if
    # anything else ever imports this module) no longer pays for a DB
    # connection + schema inspection + ALTERs as a side effect. It now
    # runs exactly once, right before the app starts accepting traffic —
    # same cold-start cost either way, but it no longer blocks module
    # import itself or runs a second time under a test client.
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    yield


app = FastAPI(
    title="Zafar API",
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)

# Paper/book list JSON compresses ~85% — a real win on slow mobile connections.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Browser origins are deployment configuration; the canonical production
# origin is https://studymw.netlibrary.dpdns.org. Bearer tokens do not use cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["X-Error-Code", "X-Attempts-Remaining", "Retry-After", "X-Total-Count"],
)


@app.middleware("http")
async def private_api_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/auth/", "/drive/")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    headers = dict(exc.headers or {})
    code = getattr(exc, "code", None)
    if code:
        headers.setdefault("X-Error-Code", code)
    if exc.status_code == 429:
        headers.setdefault("Retry-After", "60")
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=headers)

app.include_router(auth_router.router)
app.include_router(drive_router.router)


@app.exception_handler(OperationalError)
async def db_unreachable_handler(request: Request, exc: OperationalError):
    # Without this, a dead DB connection crashes the whole ASGI app with a
    # raw traceback (see the "connection timed out" incident) instead of
    # a normal HTTP response — the frontend just hangs on "Please wait...".
    # This turns that into a clean, expected error the client can show.
    return JSONResponse(
        status_code=503,
        content={"detail": "Database temporarily unreachable — please try again shortly"},
        headers={"X-Error-Code": "db_unreachable", "Cache-Control": "no-store"},
    )


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}


@app.api_route("/health/db", methods=["GET", "HEAD"])
def health_db(db: Session = Depends(get_db)):
    """
    Separate from /health on purpose — this one actually touches the
    database, so it's useful for checking that the DB is reachable. A dead
    DB here returns the clean 503 from db_unreachable_handler above instead
    of just quietly failing. Don't point an uptime pinger at it on Neon
    (it keeps the compute awake and burns CU-hours); on D1 it's harmless.
    """
    db.execute(text("SELECT 1"))
    return {"status": "ok", "db": "reachable"}
