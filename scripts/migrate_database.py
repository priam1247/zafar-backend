"""
One-time copy of the existing database into a new target.

Sources: any SQLAlchemy URL the app can read, e.g. the current Neon Postgres.
Targets: a Cloudflare D1 database ("d1") or a new local SQLite file.

Usage:
    # Neon -> D1 (needs CLOUDFLARE_ACCOUNT_ID, D1_DATABASE_ID, CLOUDFLARE_API_TOKEN set)
    python scripts/migrate_database.py \
        --source "postgresql://user:pass@host/db?sslmode=require" \
        --target d1

    # Neon -> local SQLite file
    python scripts/migrate_database.py \
        --source "postgresql://user:pass@host/db?sslmode=require" \
        --target /var/lib/zafar/zafar.db

Safety rules:
  * Refuses to write into a target that already contains rows, so it can't
    silently merge into a live database.
  * Verifies row counts on both sides after copying.
  * Reads the source only (SELECT). Nothing on the source side is changed.
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.schema import MetaData  # noqa: E402

BATCH_SIZE = 500


def _count(conn, table) -> int:
    return conn.execute(select(func.count()).select_from(table)).scalar_one()


def copy_database(source: Engine, target: Engine, metadata: MetaData) -> dict:
    """Copy every table in `metadata` from source to target.

    Tables are copied in foreign-key order and keep their original ids, so
    download_logs.user_id still points at the right users. Returns a dict of
    {table_name: rows_copied}.
    """
    metadata.create_all(bind=target)

    with target.connect() as tconn:
        for table in metadata.sorted_tables:
            existing = _count(tconn, table)
            if existing:
                raise RuntimeError(
                    f"target table '{table.name}' already has {existing} rows; "
                    "refusing to merge into a non-empty database"
                )

    copied: dict = {}
    with source.connect() as sconn:
        for table in metadata.sorted_tables:
            rows = sconn.execute(select(table)).mappings().all()
            copied[table.name] = len(rows)
            with target.begin() as tconn:
                for start in range(0, len(rows), BATCH_SIZE):
                    batch = [dict(r) for r in rows[start:start + BATCH_SIZE]]
                    if batch:
                        tconn.execute(table.insert(), batch)

    with source.connect() as sconn, target.connect() as tconn:
        for table in metadata.sorted_tables:
            src_n = _count(sconn, table)
            dst_n = _count(tconn, table)
            if src_n != dst_n:
                raise RuntimeError(
                    f"row count mismatch in '{table.name}': source={src_n} target={dst_n}"
                )

    return copied


def _target_engine(target: str) -> Engine:
    if target == "d1":
        import d1_dbapi
        from config import settings

        return d1_dbapi.make_engine(
            d1_dbapi.D1HTTPTransport(
                settings.cloudflare_account_id,
                settings.d1_database_id,
                settings.cloudflare_api_token,
            )
        )
    path = os.path.abspath(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy app data into a new database.")
    parser.add_argument("--source", required=True, help="SQLAlchemy URL to read from")
    parser.add_argument("--target", required=True, help="'d1' or a path to a new SQLite file")
    args = parser.parse_args()

    # config.py requires JWT_SECRET_KEY at import time. The migration never
    # uses it, so supply a placeholder if the environment doesn't have one.
    os.environ.setdefault("JWT_SECRET_KEY", "migration-only-unused")

    import models  # noqa: F401 — registers tables on Base.metadata
    from database import Base

    source_engine = create_engine(args.source, pool_pre_ping=True)
    target_engine = _target_engine(args.target)

    copied = copy_database(source_engine, target_engine, Base.metadata)
    for name, n in copied.items():
        print(f"{name}: {n} rows copied and verified")
    print(f"done. Target: {args.target}")


if __name__ == "__main__":
    main()
