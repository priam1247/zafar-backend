from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

from config import settings

is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False, "timeout": 15} if is_sqlite else {},
    pool_pre_ping=not is_sqlite,
)

if is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")    # readers don't block writers
        cur.execute("PRAGMA synchronous=NORMAL")  # safe under WAL, ~10x faster writes
        cur.execute("PRAGMA cache_size=-8000")    # up to 8MB page cache
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA busy_timeout=15000")  # wait instead of "database is locked"
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
