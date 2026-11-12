import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from starlette.requests import Request


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_six_simultaneous_downloads_cannot_exceed_quota(
    client, fake_drive_tree, verified_user_token, monkeypatch, tmp_path
):
    """The conditional INSERT atomically reserves exactly the remaining slots."""
    import main
    import models
    from database import get_db
    from jose import jwt
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "concurrent-downloads.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 20},
    )

    @event.listens_for(engine, "connect")
    def sqlite_wal(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA journal_mode=WAL")
        dbapi_connection.execute("PRAGMA busy_timeout=20000")

    models.Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    claims = jwt.get_unverified_claims(verified_user_token)
    with SessionLocal() as db:
        db.add(models.User(
            id=claims["uid"],
            username=claims["sub"],
            name="Test Student",
            email="student@gmail.com",
            hashed_password="unused",
            is_verified=True,
        ))
        db.commit()

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db] = override_db
    headers = auth_headers(verified_user_token)

    def download(_):
        return client.post("/drive/download/file-1?section=papers", headers=headers).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(download, range(6)))

    assert statuses.count(200) == 3
    assert statuses.count(429) == 3
    with SessionLocal() as db:
        rows = db.query(models.DownloadLog).filter_by(user_id=claims["uid"], category="paper").count()
    assert rows == 3
    engine.dispose()


def test_drive_failures_do_not_echo_folder_or_project_ids(client, fake_drive_tree, verified_user_token, monkeypatch):
    import routers.drive as drive_router

    def failed_walk(*args, **kwargs):
        raise RuntimeError("Drive project secret-project-123 folder secret-folder-456")

    monkeypatch.setattr(drive_router, "list_files_recursive", failed_walk)
    response = client.get("/drive/papers", headers=auth_headers(verified_user_token))
    assert response.status_code == 502
    assert response.json()["detail"] == "The library could not be reached."
    assert "secret-project-123" not in response.text
    assert "secret-folder-456" not in response.text


def test_account_rate_limits_are_separate_behind_a_shared_proxy():
    from rate_limit import limit_request

    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/drive/papers",
        "headers": [],
        "client": ("shared-proxy", 1234),
        "server": ("api", 443),
        "scheme": "https",
        "query_string": b"",
    })
    limit_request(request, "test-shared-proxy", "user-1", limit=1, window=60, ip_limit=20)
    limit_request(request, "test-shared-proxy", "user-2", limit=1, window=60, ip_limit=20)
    try:
        limit_request(request, "test-shared-proxy", "user-1", limit=1, window=60, ip_limit=20)
    except HTTPException as exc:
        assert exc.status_code == 429
    else:
        raise AssertionError("the same account should have exhausted its own bucket")


def test_password_reset_revokes_tokens_issued_before_reset(client, db_session):
    client.post("/auth/register", json={"name": "Reset Test", "email": "reset-session@gmail.com", "password": "before-pass"})
    from models import User

    db = db_session()
    user = db.query(User).filter_by(email="reset-session@gmail.com").first()
    user.is_verified = True
    user.reset_code = "123456"
    user.reset_code_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    db.commit()
    db.close()

    login = client.post("/auth/login", data={"email": "reset-session@gmail.com", "password": "before-pass"})
    token = login.json()["access_token"]
    reset = client.post("/auth/reset-password", json={
        "email": "reset-session@gmail.com",
        "code": "123456",
        "new_password": "after-pass",
    })
    assert reset.status_code == 200
    old_session = client.get("/auth/me", headers=auth_headers(token))
    assert old_session.status_code == 401

    new_login = client.post("/auth/login", data={"email": "reset-session@gmail.com", "password": "after-pass"})
    assert new_login.status_code == 200
    assert client.get("/auth/me", headers=auth_headers(new_login.json()["access_token"])).status_code == 200


def test_forwarded_for_spoofing_does_not_change_socket_ip_bucket():
    from rate_limit import client_ip, limit_request

    def request_with_xff(value):
        return Request({
            "type": "http", "method": "GET", "path": "/", "scheme": "http",
            "headers": [(b"x-forwarded-for", value.encode())],
            "client": ("same-socket", 9000), "server": ("api", 80), "query_string": b"",
        })

    first = request_with_xff("203.0.113.10")
    second = request_with_xff("198.51.100.20")
    assert client_ip(first) == client_ip(second) == "same-socket"
    limit_request(first, "forged-xff-test", limit=1, window=60)
    try:
        limit_request(second, "forged-xff-test", limit=1, window=60)
    except HTTPException as exc:
        assert exc.status_code == 429
    else:
        raise AssertionError("forged X-Forwarded-For must not create a second bucket")


def test_private_docs_are_disabled_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_drive_health_returns_counts_not_names_or_ids(client, verified_user_token, monkeypatch):
    import routers.drive as drive_router

    monkeypatch.setattr(drive_router, "list_children", lambda _root: [
        {"id": "hidden-folder-id", "name": "Sensitive Name", "mimeType": "application/vnd.google-apps.folder"},
        {"id": "hidden-file-id", "name": "Private File", "mimeType": "application/pdf"},
    ])
    response = client.get("/drive/health", headers=auth_headers(verified_user_token))
    assert response.status_code == 200
    assert response.json() == {"connected": True, "top_level_items_count": 2, "folders_count": 1}
    assert "hidden-folder-id" not in response.text and "Sensitive Name" not in response.text


def test_user_out_does_not_expose_internal_username(client, verified_user_token):
    response = client.get("/auth/me", headers=auth_headers(verified_user_token))
    assert response.status_code == 200
    assert "username" not in response.json()


def test_worker_rejects_html_without_caching_and_forwards_range():
    from pathlib import Path

    worker = Path("cloudflare-worker/download-proxy/worker.js").read_text()
    assert 'function isHtmlResponse(response)' in worker
    assert 'errorResponse("Upstream returned a non-file page", 502)' in worker
    assert 'request.headers.get("Range")' in worker
    assert 'upstreamHeaders.set("Range", range)' in worker
    assert "cacheEverything: true" not in worker
    assert "cacheTtl: 86400" not in worker


def test_drive_depth_limit_logs_truncated_folder_id(monkeypatch, caplog):
    import drive_service
    from config import settings

    monkeypatch.setattr(settings, "drive_max_depth", 1)
    monkeypatch.setattr(drive_service, "clear_cache", drive_service.clear_cache)
    drive_service.clear_cache()
    children = {
        "root": [{"id": "folder-1", "name": "One", "mimeType": "application/vnd.google-apps.folder"}],
        "folder-1": [{"id": "folder-2-secret", "name": "Two", "mimeType": "application/vnd.google-apps.folder"}],
    }
    monkeypatch.setattr(drive_service, "list_children", lambda folder: children.get(folder, []))
    with caplog.at_level(logging.WARNING, logger="drive_service"):
        drive_service.list_files_recursive("root", force_refresh=True)
    assert "folder-2-secret" in caplog.text
    drive_service.clear_cache()


def test_error_code_is_carried_by_exception_not_english_copy():
    from errors import APIError
    from main import http_error_handler

    request = Request({
        "type": "http", "method": "GET", "path": "/", "scheme": "http",
        "headers": [], "client": ("test", 1), "server": ("api", 80), "query_string": b"",
    })
    response = asyncio.run(http_error_handler(request, APIError(403, "Changed copy", "email_not_verified")))
    assert response.headers["x-error-code"] == "email_not_verified"
    plain = asyncio.run(http_error_handler(request, HTTPException(403, "Email not verified")))
    assert "x-error-code" not in plain.headers


def test_section_id_cache_uses_tree_ttl(monkeypatch):
    import routers.drive as drive_router
    from config import settings

    clock = [100.0]
    monkeypatch.setattr(settings, "drive_cache_ttl_seconds", 5)
    monkeypatch.setattr(drive_router, "time", SimpleNamespace(time=lambda: clock[0]))
    calls = []
    monkeypatch.setattr(drive_router, "find_child_folder", lambda root, name: calls.append(name) or "folder-id")
    monkeypatch.setattr(drive_router, "_section_id_cache", {})
    assert drive_router._section_folder_id("papers") == "folder-id"
    clock[0] = 104.0
    assert drive_router._section_folder_id("papers") == "folder-id"
    clock[0] = 106.0
    assert drive_router._section_folder_id("papers") == "folder-id"
    assert len(calls) == 2


def test_email_sends_are_counted_warned_and_capped(client, monkeypatch, caplog):
    from config import settings

    monkeypatch.setattr(settings, "email_daily_limit", 2)
    monkeypatch.setattr(settings, "email_daily_warn_threshold", 1)
    with caplog.at_level(logging.WARNING, logger="email_utils"):
        first = client.post("/auth/register", json={"name": "One", "email": "one-cap@gmail.com", "password": "pass123"})
        second = client.post("/auth/register", json={"name": "Two", "email": "two-cap@gmail.com", "password": "pass123"})
        capped = client.post("/auth/register", json={"name": "Three", "email": "three-cap@gmail.com", "password": "pass123"})
    assert first.status_code == second.status_code == 201
    assert "Gmail daily send counter reached" in caplog.text
    assert capped.status_code == 429
    assert capped.headers["retry-after"]


def test_registration_and_resend_return_503_on_smtp_failure(client, monkeypatch):
    import email_utils

    def fail(_email, _code):
        raise email_utils.EmailDeliveryError("provider internals should not reach the response")

    monkeypatch.setattr(email_utils, "send_code", fail)
    registered = client.post("/auth/register", json={"name": "SMTP Test", "email": "smtp-fail@gmail.com", "password": "pass123"})
    assert registered.status_code == 503
    assert registered.headers["x-error-code"] == "email_delivery_failed"
    resent = client.post("/auth/resend", json={"email": "smtp-fail@gmail.com"})
    assert resent.status_code == 503
    assert "provider internals" not in resent.text


def test_register_conflict_codes_distinguish_verified_and_unverified(client, db_session):
    client.post("/auth/register", json={"name": "Conflict", "email": "conflict@gmail.com", "password": "pass123"})
    unverified = client.post("/auth/register", json={"name": "Conflict", "email": "conflict@gmail.com", "password": "pass123"})
    assert unverified.status_code == 409
    assert unverified.headers["x-error-code"] == "unverified"

    from models import User
    db = db_session()
    db.query(User).filter_by(email="conflict@gmail.com").update({"is_verified": True})
    db.commit()
    db.close()
    verified = client.post("/auth/register", json={"name": "Conflict", "email": "conflict@gmail.com", "password": "pass123"})
    assert verified.status_code == 409
    assert verified.headers["x-error-code"] == "verified"


def test_papers_endpoint_supports_limit_and_offset(client, fake_drive_tree, verified_user_token, monkeypatch):
    import routers.drive as drive_router

    docs = [
        {"id": f"pdf-{index}", "name": f"Paper {index}.pdf", "mimeType": "application/pdf", "category": "Class", "size": "10"}
        for index in range(3)
    ]
    monkeypatch.setattr(drive_router, "list_files_recursive", lambda *_args, **_kwargs: docs)
    response = client.get("/drive/papers?section=papers&limit=1&offset=1", headers=auth_headers(verified_user_token))
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["pdf-1"]
    assert response.headers["x-total-count"] == "3"


def test_papers_total_count_covers_both_sources_and_category_filters(
    client, verified_user_token, monkeypatch
):
    import routers.drive as drive_router

    # This is the combined Google + Drime result set before server pagination.
    combined = [
        {"id": "google-1", "name": "Primary Paper 1.pdf", "mimeType": "application/pdf", "category": "Standard 8"},
        {"id": "drime-1", "name": "Primary Paper 2.pdf", "mimeType": "application/pdf", "category": "Standard 8"},
        {"id": "google-2", "name": "Other Paper.pdf", "mimeType": "application/pdf", "category": "Standard 7"},
    ]
    monkeypatch.setattr(drive_router, "_list_section_files", lambda *_args, **_kwargs: combined)

    response = client.get(
        "/drive/papers?section=primary&category=Standard%208&limit=1&offset=0",
        headers=auth_headers(verified_user_token),
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.headers["x-total-count"] == "2"


def test_drive_list_routes_key_budgets_by_user_identifier(client, verified_user_token, monkeypatch):
    import routers.drive as drive_router

    # A second account makes two requests from this same TestClient/socket.
    client.post("/auth/register", json={"name": "Second", "email": "second-list-user@gmail.com", "password": "pass123"})
    from models import User
    from database import SessionLocal

    db = SessionLocal()
    second_user = db.query(User).filter_by(email="second-list-user@gmail.com").first()
    second_user.is_verified = True
    db.commit()
    db.close()
    second_login = client.post("/auth/login", data={"email": "second-list-user@gmail.com", "password": "pass123"})
    second_token = second_login.json()["access_token"]

    identifiers = []

    def capture(_request, _scope, identifier="", **_kwargs):
        identifiers.append(identifier)

    monkeypatch.setattr(drive_router, "limit_request", capture)
    monkeypatch.setattr(drive_router, "_section_folder_id", lambda _section: "resolved-folder-id")
    monkeypatch.setattr(drive_router, "list_files_recursive", lambda *_a, **_kw: [])
    user_tokens = [verified_user_token, second_token]
    for token in user_tokens:
        headers = auth_headers(token)
        assert client.get("/drive/papers", headers=headers).status_code == 200
        assert client.get("/drive/categories", headers=headers).status_code == 200

    assert identifiers[0] != identifiers[2]
    assert identifiers[1] != identifiers[3]
    assert len(set(identifiers)) == 2
