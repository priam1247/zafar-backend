"""
Tests for the Cloudflare D1 backend.

A FakeD1 transport runs each request's SQL on a real in-memory SQLite
database and returns the same JSON shape Cloudflare's query endpoint
documents. That exercises the whole driver (SQLAlchemy -> d1_dbapi -> transport)
without network access. Only the live Cloudflare endpoint is untested here.
"""
import datetime as dt
import importlib.util
import logging
import os
import re
import sqlite3
import sys

import pytest
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.orm import sessionmaker

import d1_dbapi
from database import Base
import models  # noqa: F401 — registers tables on Base.metadata
from models import DownloadLog, User


class FakeD1(d1_dbapi._Transport):
    """Runs D1-shaped requests against a local SQLite database."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self.post_count = 0
        self.batch_count = 0

    def post(self, body: dict) -> dict:
        self.post_count += 1
        if "batch" in body:
            self.batch_count += 1
            statements = body["batch"]
        else:
            statements = [body]
        results = []
        self.db.execute("BEGIN")
        try:
            for st in statements:
                results.append(self._run(st["sql"], st.get("params", [])))
        except sqlite3.Error as exc:
            self.db.execute("ROLLBACK")
            return {"success": False, "errors": [{"message": str(exc)}], "messages": [], "result": []}
        self.db.execute("COMMIT")
        return {"success": True, "errors": [], "messages": [], "result": results}

    # D1 refuses most PRAGMAs with "not authorized: SQLITE_AUTH". This allowlist
    # is an assumption about which ones work; the app only needs table_info and
    # the index/foreign-key introspection SQLAlchemy uses.
    _ALLOWED_PRAGMAS = {"table_info", "index_list", "index_info", "foreign_key_list"}

    def _run(self, sql, params):
        match = re.match(r"^\s*PRAGMA\s+(?:(\w+)\s*\.\s*)?(\w+)", sql, re.IGNORECASE)
        if match and match.group(1) and match.group(1).lower() == "temp":
            raise sqlite3.DatabaseError("not authorized: SQLITE_AUTH")
        if match and match.group(2).lower() not in self._ALLOWED_PRAGMAS:
            raise sqlite3.DatabaseError("not authorized: SQLITE_AUTH")
        cur = self.db.execute(sql, params)
        rows = []
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {
            "success": True,
            "results": rows,
            "meta": {"changes": max(cur.rowcount, 0), "last_row_id": cur.lastrowid},
        }


@pytest.fixture()
def fake():
    return FakeD1()


@pytest.fixture()
def d1_engine(fake):
    engine = d1_dbapi.make_engine(fake)
    Base.metadata.create_all(bind=engine)
    yield engine


@pytest.fixture()
def d1_session(d1_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=d1_engine)


def test_orm_roundtrip_through_d1_driver(d1_session):
    created = dt.datetime(2026, 10, 18, 12, 0, 0)
    db = d1_session()
    user = User(username="u1", email="a@gmail.com", hashed_password="x",
                is_verified=True, created_at=created)
    db.add(user)
    db.commit()
    db.add(DownloadLog(user_id=user.id, file_id="f1", file_name="Paper.pdf",
                       category="paper", downloaded_at=created, confirmed=True))
    db.commit()
    db.close()

    db = d1_session()
    got = db.query(User).filter(User.email == "a@gmail.com").one()
    assert got.is_verified is True
    assert got.created_at == created
    count = db.query(func.count(DownloadLog.id)).filter(DownloadLog.user_id == got.id).scalar()
    assert count == 1
    db.close()


def test_unique_violation_raises_integrity_error(d1_session):
    db = d1_session()
    db.add(User(username="dup", email="dup@gmail.com", hashed_password="x"))
    db.commit()
    db.add(User(username="dup", email="other@gmail.com", hashed_password="x"))
    with pytest.raises(SAIntegrityError):
        db.commit()
    db.rollback()
    db.close()


def test_executemany_is_one_batch_request(d1_engine, fake):
    rows = [
        {"username": f"b{i}", "hashed_password": "x", "is_verified": False,
         "verification_attempts": 0, "reset_attempts": 0}
        for i in range(3)
    ]
    before = fake.batch_count
    with d1_engine.begin() as conn:
        conn.execute(User.__table__.insert(), rows)
    assert fake.batch_count == before + 1
    with d1_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(User.__table__)).scalar_one() == 3


def test_transaction_statements_are_accepted_and_ignored():
    conn = d1_dbapi.connect(transport=FakeD1())
    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.execute("COMMIT")
    cur.execute("ROLLBACK")
    conn.commit()
    conn.rollback()  # no writes yet, so no warning and no error


def test_rollback_after_write_logs_warning(caplog):
    conn = d1_dbapi.connect(transport=FakeD1())
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (x INTEGER)")
    cur.execute("INSERT INTO t (x) VALUES (?)", (1,))
    with caplog.at_level(logging.WARNING, logger="d1_dbapi"):
        conn.rollback()
    assert any("already committed" in r.message for r in caplog.records)


def test_last_row_id_is_reported_for_inserts(d1_engine):
    with d1_engine.begin() as conn:
        result = conn.execute(
            User.__table__.insert().values(username="lr", hashed_password="x",
                                           is_verified=False, verification_attempts=0,
                                           reset_attempts=0)
        )
        assert result.inserted_primary_key[0] >= 1


def test_missing_credentials_fail_clearly():
    transport = d1_dbapi.D1HTTPTransport("", "", "")
    with pytest.raises(d1_dbapi.InterfaceError, match="CLOUDFLARE_API_TOKEN"):
        transport.send({"sql": "SELECT 1", "params": []})


def test_unreachable_api_raises_operational_error():
    transport = d1_dbapi.D1HTTPTransport(
        "acc", "db", "token",
        base_url="http://127.0.0.1:9/{account_id}/{database_id}",
        timeout=2,
    )
    with pytest.raises(d1_dbapi.OperationalError):
        transport.send({"sql": "SELECT 1", "params": []})


def test_error_payload_is_classified():
    class Broken(d1_dbapi._Transport):
        def post(self, body):
            return {"success": False, "errors": [{"message": "near \"FROM\": syntax error"}]}

    with pytest.raises(d1_dbapi.ProgrammingError):
        Broken().send({"sql": "SELEC", "params": []})


def test_run_startup_migrations_on_d1(monkeypatch, d1_engine):
    import main
    monkeypatch.setattr(main, "engine", d1_engine)
    main._run_migrations()  # must not raise on a fresh D1-shaped schema


def _load_migration_module():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "migrate_database.py")
    spec = importlib.util.spec_from_file_location("migrate_database", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_copies_sqlite_data_into_d1(tmp_path, fake):
    migrate = _load_migration_module()

    source = create_engine(f"sqlite:///{tmp_path / 'src.db'}")
    Base.metadata.create_all(bind=source)
    with source.begin() as conn:
        conn.execute(insert(User), [
            {"id": 1, "username": "m1", "email": "m1@gmail.com", "hashed_password": "x",
             "is_verified": True, "verification_attempts": 0, "reset_attempts": 0},
            {"id": 2, "username": "m2", "email": "m2@gmail.com", "hashed_password": "x",
             "is_verified": False, "verification_attempts": 0, "reset_attempts": 0},
        ])
        conn.execute(insert(DownloadLog), [
            {"id": 1, "user_id": 1, "file_id": "f", "file_name": "p.pdf",
             "category": "paper", "confirmed": True},
        ])

    target = d1_dbapi.make_engine(fake)
    copied = migrate.copy_database(source, target, Base.metadata)
    assert copied == {"users": 2, "download_logs": 1}

    with target.connect() as conn:
        rows = conn.execute(select(User.id, User.email, User.is_verified).order_by(User.id)).all()
        assert [tuple(r) for r in rows] == [(1, "m1@gmail.com", True), (2, "m2@gmail.com", False)]
        log_user = conn.execute(select(DownloadLog.user_id)).scalar_one()
        assert log_user == 1  # foreign key preserved


def test_migration_refuses_nonempty_target(tmp_path, fake):
    migrate = _load_migration_module()

    source = create_engine(f"sqlite:///{tmp_path / 'src2.db'}")
    Base.metadata.create_all(bind=source)

    target = d1_dbapi.make_engine(fake)
    Base.metadata.create_all(bind=target)
    with target.begin() as conn:
        conn.execute(insert(User), [{"username": "exists", "hashed_password": "x",
                                     "is_verified": False, "verification_attempts": 0,
                                     "reset_attempts": 0}])

    with pytest.raises(RuntimeError, match="refusing to merge"):
        migrate.copy_database(source, target, Base.metadata)


def test_read_uncommitted_pragma_is_answered_locally(fake):
    """SQLAlchemy runs PRAGMA read_uncommitted on every new connection. D1 refuses
    it, so the driver must answer it itself (0 = read committed)."""
    conn = d1_dbapi.connect(transport=fake)
    cur = conn.cursor()
    cur.execute("PRAGMA read_uncommitted")
    assert cur.fetchone() == (0,)


def test_engine_connects_when_d1_refuses_pragmas(fake):
    """Regression: the first connection (dialect initialise) must not fail
    with 'not authorized: SQLITE_AUTH' from PRAGMA read_uncommitted."""
    engine = d1_dbapi.make_engine(fake)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT 1").scalar_one() == 1


def test_other_refused_pragmas_still_fail_honestly(fake):
    conn = d1_dbapi.connect(transport=fake)
    cur = conn.cursor()
    with pytest.raises(d1_dbapi.OperationalError, match="SQLITE_AUTH"):
        cur.execute("PRAGMA journal_mode=WAL")


def test_table_xinfo_is_served_from_table_info(fake):
    """SQLAlchemy 2.x uses PRAGMA table_xinfo for get_columns on every startup.
    The driver must send table_info (which D1 allows) and add the 'hidden' column."""
    conn = d1_dbapi.connect(transport=fake)
    cur = conn.cursor()
    cur.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)')
    cur.execute('PRAGMA main.table_xinfo("t")')
    names = [d[0] for d in cur.description]
    assert names == ["cid", "name", "type", "notnull", "dflt_value", "pk", "hidden"]
    rows = cur.fetchall()
    assert [r[1] for r in rows] == ["id", "name"]
    assert all(r[-1] == 0 for r in rows)


def test_create_all_on_empty_d1_probes_temp_schema(fake):
    """Regression: on a fresh D1 database, SQLAlchemy's has_table() asks for
    PRAGMA main.table_info (empty), then PRAGMA temp.table_info. D1 refuses the
    temp one with SQLITE_AUTH, which crashed Koyeb startup."""
    engine = d1_dbapi.make_engine(fake)
    Base.metadata.create_all(bind=engine)  # must not raise
    with engine.connect() as conn:
        names = {r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "download_logs"} <= names


def test_has_table_is_false_for_missing_table_without_temp_error(fake):
    from sqlalchemy import inspect
    engine = d1_dbapi.make_engine(fake)
    assert inspect(engine).has_table("users") is False
