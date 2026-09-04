import os
import sys

import pytest

# Must be set before `config` (and anything importing it) loads, since
# pydantic-settings reads the environment at import time.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-do-not-use-in-prod")
os.environ.setdefault("DRIVE_ROOT_FOLDER_ID", "root-folder-id")
os.environ.setdefault("DAILY_PAPER_LIMIT", "3")
os.environ.setdefault("DAILY_BOOK_LIMIT", "2")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402


@pytest.fixture()
def db_session(monkeypatch):
    """
    Fresh in-memory SQLite per test, wired into database.SessionLocal so
    every app module (which imported get_db already) uses it.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)

    import models  # noqa: F401 — registers tables on Base.metadata
    database.Base.metadata.create_all(bind=engine)

    yield TestingSessionLocal
    database.Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db_session):
    import main  # imported here so it binds to the patched engine/session
    from database import get_db

    def _override_get_db():
        db = db_session()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db] = _override_get_db
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture()
def fake_drive_tree(monkeypatch):
    """
    Stands in for drive_service.list_files_recursive so tests never hit
    the real Google Drive API / need real service-account credentials.
    """
    import drive_service
    import routers.drive as drive_router

    files = [
        {"id": "file-1", "name": "Paper One.pdf", "mimeType": "application/pdf",
         "size": "1000", "category": "MSCE Maneb"},
        {"id": "file-2", "name": "Book One.pdf", "mimeType": "application/pdf",
         "size": "2000", "category": "Books"},
    ]

    calls = {"count": 0}

    def _fake_list_files_recursive(folder_id, max_depth=6, force_refresh=False):
        calls["count"] += 1
        return files

    monkeypatch.setattr(drive_service, "list_files_recursive", _fake_list_files_recursive)
    monkeypatch.setattr(drive_router, "list_files_recursive", _fake_list_files_recursive)
    monkeypatch.setattr(drive_router, "_section_folder_id", lambda section: "resolved-folder-id")
    return calls


@pytest.fixture()
def verified_user_token(client, db_session):
    """Registers + verifies a user, returns a valid bearer token string."""
    client.post("/auth/register", json={"email": "student@gmail.com", "password": "hunter22"})

    from models import User
    db = db_session()
    user = db.query(User).filter(User.email == "student@gmail.com").first()
    code = user.verification_code
    db.close()

    client.post("/auth/verify", json={"email": "student@gmail.com", "code": code})
    resp = client.post(
        "/auth/login",
        data={"email": "student@gmail.com", "password": "hunter22"},
    )
    return resp.json()["access_token"]
