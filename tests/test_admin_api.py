import base64
import hashlib
import hmac
import json
import struct
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from conftest import auth_headers
from jose import jwt


def _code(secret, now=None):
    step = int((time.time() if now is None else now) // 30)
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


def _make_user(db_session, *, email, role=None, verified=True, suspended=False, totp=False):
    from admin_security import encrypt_totp_secret, new_totp_secret
    from auth import hash_password
    from models import User

    db = db_session()
    secret = new_totp_secret() if totp else None
    user = User(
        email=email,
        username=email.split("@", 1)[0],
        name=email.split("@", 1)[0].title(),
        hashed_password=hash_password("strong-password"),
        is_verified=verified,
        role=role,
        suspended=suspended,
        signup_method="email",
        admin_totp_secret=encrypt_totp_secret(secret) if secret else None,
        admin_totp_enabled=totp,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    user_id = user.id
    db.close()
    return user_id, secret


def _token(user_id, db_session, *, mfa=False):
    from auth import create_access_token
    from models import User

    db = db_session()
    token = create_access_token(db.query(User).filter_by(id=user_id).one(), mfa_verified=mfa)
    db.close()
    return token


def test_admin_requires_admin_role_and_enforces_viewer_scope(client, db_session):
    student_id, _ = _make_user(db_session, email="student-admin-test@gmail.com")
    viewer_id, _ = _make_user(db_session, email="viewer-admin-test@gmail.com", role="viewer")
    assert client.get("/admin/whoami", headers=auth_headers(_token(student_id, db_session))).status_code == 403
    headers = auth_headers(_token(viewer_id, db_session))
    assert client.get("/admin/whoami", headers=headers).json()["role"] == "viewer"
    assert client.get("/admin/overview", headers=headers).status_code == 200
    denied = client.get("/admin/users", headers=headers)
    assert denied.status_code == 403
    assert denied.headers["x-error-code"] == "admin_forbidden"


def test_owner_users_search_returns_real_rows_and_total(client, db_session):
    owner_id, _ = _make_user(db_session, email="owner-users@gmail.com", role="owner", totp=True)
    target_id, _ = _make_user(db_session, email="amina.real@gmail.com")
    db = db_session()
    from models import DownloadLog
    db.add(DownloadLog(user_id=target_id, file_id="real-file", file_name="Actual.pdf", category="paper", confirmed=True))
    db.commit()
    db.close()
    response = client.get("/admin/users?q=amina&limit=10&offset=0", headers=auth_headers(_token(owner_id, db_session, mfa=True)))
    assert response.status_code == 200
    assert response.headers["x-total-count"] == "1"
    assert response.json()[0]["email"] == "amina.real@gmail.com"
    assert response.json()[0]["downloads"] == 1
    assert response.json()[0]["signup_method"] == "email"


def test_role_management_matches_frontend_conflict_codes_and_audits_success(client, db_session):
    owner_id, _ = _make_user(db_session, email="owner-role-admin@gmail.com", role="owner", totp=True)
    target_id, _ = _make_user(db_session, email="target-role-admin@gmail.com")
    suspended_id, _ = _make_user(
        db_session, email="suspended-role-admin@gmail.com", role="viewer", suspended=True, totp=True,
    )
    headers = auth_headers(_token(owner_id, db_session, mfa=True))

    missing_totp = client.patch(f"/admin/users/{target_id}", json={"role": "owner"}, headers=headers)
    assert missing_totp.status_code == 409
    assert missing_totp.headers["x-error-code"] == "owner_mfa_not_enrolled"

    suspended = client.patch(f"/admin/users/{suspended_id}", json={"role": "owner"}, headers=headers)
    assert suspended.status_code == 409
    assert suspended.headers["x-error-code"] == "owner_account_suspended"

    last_owner = client.patch(f"/admin/users/{owner_id}", json={"role": "editor"}, headers=headers)
    assert last_owner.status_code == 409
    assert last_owner.headers["x-error-code"] == "last_owner"

    other_owner_id, _ = _make_user(db_session, email="other-role-owner@gmail.com", role="owner", totp=True)
    self_change = client.patch(f"/admin/users/{owner_id}", json={"role": "viewer"}, headers=headers)
    assert self_change.status_code == 409
    assert self_change.headers["x-error-code"] == "self_role_change"

    changed = client.patch(f"/admin/users/{target_id}", json={"role": "editor"}, headers=headers)
    assert changed.status_code == 200
    assert changed.json()["role"] == "editor"
    audit = client.get("/admin/audit?limit=50", headers=headers)
    assert any(
        row["action"] == "user.role" and row["target"] == str(target_id)
        and json.loads(row["detail"]) == {"from": None, "to": "editor"}
        for row in audit.json()
    )
    # The second owner remains unchanged and access is still role-scoped.
    assert client.get("/admin/whoami", headers=auth_headers(_token(other_owner_id, db_session, mfa=True))).status_code == 200


def test_suspend_mutation_is_immediate_and_audited(client, db_session):
    owner_id, _ = _make_user(db_session, email="owner-suspend@gmail.com", role="owner", totp=True)
    target_id, _ = _make_user(db_session, email="target-suspend@gmail.com")
    token = _token(owner_id, db_session, mfa=True)
    response = client.post(f"/admin/users/{target_id}/suspend", json={"suspended": True, "reason": "verified report"}, headers=auth_headers(token))
    assert response.status_code == 200
    assert response.json()["suspended"] is True
    denied = client.get("/auth/me", headers=auth_headers(_token(target_id, db_session)))
    assert denied.status_code == 403
    assert denied.headers["x-error-code"] == "user_suspended"
    audit = client.get("/admin/audit?limit=50", headers=auth_headers(token))
    assert any(row["action"] == "user.suspend" and row["target"] == str(target_id) for row in audit.json())


def test_owner_cannot_suspend_or_delete_own_session(client, db_session):
    owner_id, _ = _make_user(db_session, email="only-owner@gmail.com", role="owner", totp=True)
    headers = auth_headers(_token(owner_id, db_session, mfa=True))
    suspend = client.post(f"/admin/users/{owner_id}/suspend", json={"suspended": True}, headers=headers)
    assert suspend.status_code == 409
    assert suspend.headers["x-error-code"] == "self_suspend"
    delete = client.delete(f"/admin/users/{owner_id}", headers=headers)
    assert delete.status_code == 409
    assert delete.headers["x-error-code"] == "self_delete"


def test_admin_settings_are_persisted_and_change_live_quota(client, db_session, fake_drive_tree):
    owner_id, _ = _make_user(db_session, email="owner-settings@gmail.com", role="owner", totp=True)
    headers = auth_headers(_token(owner_id, db_session, mfa=True))
    response = client.patch("/admin/settings", json={
        "announcement": "New papers added", "maintenance": True,
        "featured": ["file-1"], "limits": {"papers": 7},
    }, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["announcement"] == "New papers added"
    assert body["maintenance"] is True
    assert body["featured"] == ["file-1"]
    assert body["limits"]["papers"] == 7
    assert client.get("/auth/quota", headers=headers).json()["papers_limit"] == 7
    assert client.get("/site-config").json() == {
        "announcement": "New papers added", "maintenance": True, "featured": ["file-1"],
    }


def test_featured_setting_rejects_ids_not_in_live_library(client, db_session, fake_drive_tree):
    owner_id, _ = _make_user(db_session, email="owner-featured@gmail.com", role="owner", totp=True)
    response = client.patch("/admin/settings", json={"featured": ["fabricated-id"]},
                            headers=auth_headers(_token(owner_id, db_session, mfa=True)))
    assert response.status_code == 422
    assert response.headers["x-error-code"] == "admin_featured_file_missing"


def test_overview_counts_real_rows_zero_fills_days_and_omits_unknown_subjects(client, db_session):
    owner_id, _ = _make_user(db_session, email="owner-overview@gmail.com", role="owner", totp=True)
    db = db_session()
    from models import DownloadLog, SearchLog, User
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    user = User(email="real-student@gmail.com", username="real-student", name="Real Student", hashed_password="x",
                is_verified=True, created_at=today, signup_method="google")
    db.add(user)
    db.commit()
    db.refresh(user)
    db.add(DownloadLog(user_id=user.id, file_id="real-paper", file_name="MSCE Mathematics 2024.pdf",
                       category="paper", downloaded_at=today, confirmed=True))
    db.add(SearchLog(q="missing biology 2030", results=0, section="papers", at=today))
    db.commit()
    db.close()
    response = client.get("/admin/overview?days=3", headers=auth_headers(_token(owner_id, db_session, mfa=True)))
    assert response.status_code == 200
    body = response.json()
    assert body["users"]["total"] == 2
    assert body["users"]["verified"] == 2
    assert body["downloads"]["today"] == 1
    assert len(body["daily"]) == 3
    assert sum(day["downloads"] for day in body["daily"]) == 1
    assert body["top_files"] == [{"id": "real-paper", "name": "MSCE Mathematics 2024.pdf", "section": "papers", "downloads": 1}]
    assert body["zero_result_searches"] == [{"q": "missing biology 2030", "count": 1}]
    assert "top_subjects" not in body


def test_student_request_submission_enters_the_real_admin_queue(client, db_session, verified_user_token):
    owner_id, _ = _make_user(db_session, email="owner-intake@gmail.com", role="owner", totp=True)
    submitted = client.post(
        "/requests",
        json={"section": "primary", "title": "  Standard 8 science  ", "note": " Please add term two."},
        headers=auth_headers(verified_user_token),
    )
    assert submitted.status_code == 201
    assert submitted.headers["cache-control"] == "no-store"
    assert submitted.json()["title"] == "Standard 8 science"
    assert submitted.json()["status"] == "open"

    queued = client.get("/admin/requests?status=open", headers=auth_headers(_token(owner_id, db_session, mfa=True)))
    assert len(queued.json()) == 1
    assert queued.json()[0]["email"] == "student@gmail.com"
    assert queued.json()[0]["section"] == "primary"


def test_student_report_requires_a_real_library_file_and_uses_catalogue_name(
    client, db_session, fake_drive_tree, verified_user_token,
):
    owner_id, _ = _make_user(db_session, email="owner-report-intake@gmail.com", role="owner", totp=True)
    spoofed = client.post(
        "/reports", json={"file_id": "file-1", "reason": "broken download", "file_name": "Fake.pdf"},
        headers=auth_headers(verified_user_token),
    )
    assert spoofed.status_code == 422

    missing = client.post(
        "/reports", json={"file_id": "invented-file", "reason": "broken"},
        headers=auth_headers(verified_user_token),
    )
    assert missing.status_code == 404
    assert missing.headers["x-error-code"] == "report_file_missing"

    submitted = client.post(
        "/reports", json={"file_id": "file-1", "reason": "broken download", "note": "  Link gives an error.  "},
        headers=auth_headers(verified_user_token),
    )
    assert submitted.status_code == 201
    assert submitted.headers["cache-control"] == "no-store"
    assert submitted.json()["file_name"] == "Paper One.pdf"
    assert submitted.json()["note"] == "Link gives an error."

    queued = client.get("/admin/reports?status=open", headers=auth_headers(_token(owner_id, db_session, mfa=True)))
    assert len(queued.json()) == 1
    assert queued.json()[0]["email"] == "student@gmail.com"
    assert queued.json()[0]["file_name"] == "Paper One.pdf"


def test_student_intake_limits_are_per_account_not_shared_ip(client, db_session, verified_user_token):
    headers = auth_headers(verified_user_token)
    for index in range(3):
        response = client.post(
            "/requests", json={"section": "papers", "title": f"Missing paper {index}"}, headers=headers,
        )
        assert response.status_code == 201
    limited = client.post("/requests", json={"section": "papers", "title": "One more"}, headers=headers)
    assert limited.status_code == 429
    assert limited.headers["x-error-code"] == "rate_limited"

    other_id, _ = _make_user(db_session, email="other-student-intake@gmail.com")
    other_response = client.post(
        "/requests", json={"section": "papers", "title": "Another student's request"},
        headers=auth_headers(_token(other_id, db_session)),
    )
    assert other_response.status_code == 201


def test_editor_can_resolve_real_request_and_report(client, db_session):
    editor_id, _ = _make_user(db_session, email="editor-queue@gmail.com", role="editor")
    db = db_session()
    from models import AdminReport, AdminRequest
    req = AdminRequest(email="student@gmail.com", section="papers", title="Missing 2028 paper", note="Real report")
    report = AdminReport(email="student@gmail.com", file_id="file123", file_name="Broken.pdf", reason="404")
    db.add_all([req, report])
    db.commit()
    req_id, report_id = req.id, report.id
    db.close()
    headers = auth_headers(_token(editor_id, db_session))
    assert client.get("/admin/requests?status=open", headers=headers).json()[0]["title"] == "Missing 2028 paper"
    assert client.patch(f"/admin/requests/{req_id}", json={"status": "done"}, headers=headers).json()["status"] == "done"
    assert client.patch(f"/admin/reports/{report_id}", json={"status": "resolved"}, headers=headers).json()["status"] == "resolved"
    assert client.get("/admin/settings", headers=headers).status_code == 403


def test_noninteractive_cli_reenrolls_owner_for_hosted_console(client, db_session, monkeypatch, capsys):
    owner_id, _ = _make_user(db_session, email="owner-console@gmail.com", role="owner", totp=True)
    import scripts.make_owner as owner_cli
    from admin_security import decrypt_totp_secret

    monkeypatch.setattr(owner_cli, "SessionLocal", db_session)
    monkeypatch.setattr(
        "sys.argv",
        ["make_owner", "--enroll", "--non-interactive", "owner-console@gmail.com"],
    )
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(AssertionError("must not prompt")))
    owner_cli.main()
    output = capsys.readouterr().out
    assert "saved without interactive verification" in output

    db = db_session()
    from models import AdminAuditLog, User
    owner = db.query(User).filter_by(id=owner_id).one()
    assert owner.role == "owner"
    assert owner.admin_totp_enabled is True
    secret = decrypt_totp_secret(owner.admin_totp_secret)
    assert secret in output
    assert db.query(AdminAuditLog).filter_by(action="admin.totp_enroll", target=str(owner_id)).count() == 1
    db.close()

    login = client.post(
        "/auth/login",
        data={"email": "owner-console@gmail.com", "password": "strong-password", "totp_code": _code(secret)},
    )
    assert login.status_code == 200


def test_owner_login_requires_fresh_non_reused_totp(client, db_session):
    owner_id, secret = _make_user(db_session, email="owner-mfa@gmail.com", role="owner", totp=True)
    password = "strong-password"
    missing = client.post("/auth/login", data={"email": "owner-mfa@gmail.com", "password": password})
    assert missing.status_code == 401
    assert missing.headers["x-error-code"] == "admin_totp_required"
    code = _code(secret)
    login = client.post("/auth/login", data={"email": "owner-mfa@gmail.com", "password": password, "totp_code": code})
    assert login.status_code == 200
    claims = jwt.decode(login.json()["access_token"], "test-secret-do-not-use-in-prod", algorithms=["HS256"])
    assert claims["mfa"] is True
    replay = client.post("/auth/login", data={"email": "owner-mfa@gmail.com", "password": password, "totp_code": code})
    assert replay.status_code == 401
    assert replay.headers["x-error-code"] == "admin_totp_required"


def test_owner_export_uses_expiring_signed_link_and_sanitizes_csv(client, db_session):
    owner_id, _ = _make_user(db_session, email="owner-export@gmail.com", role="owner", totp=True)
    _make_user(db_session, email='=HYPERLINK("bad")@gmail.com')
    headers = auth_headers(_token(owner_id, db_session, mfa=True))
    created = client.post("/admin/export/users", json={}, headers=headers)
    assert created.status_code == 200
    data = urlsplit(created.json()["url"])
    exported = client.get(data.path + "?" + data.query)
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK" in exported.text
    assert "Authorization" not in data.query
    tampered = client.get(data.path + "?" + data.query.replace("kind=users", "kind=files"))
    assert tampered.status_code == 403
    audit = client.get("/admin/audit", headers=headers)
    assert any(row["action"] == "export.create" for row in audit.json())


def test_admin_no_store_and_noindex_headers(client, db_session):
    viewer_id, _ = _make_user(db_session, email="viewer-headers@gmail.com", role="viewer")
    response = client.get("/admin/whoami", headers=auth_headers(_token(viewer_id, db_session)))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_zero_result_search_is_recorded_from_real_drive_response(client, db_session, fake_drive_tree, verified_user_token):
    response = client.get("/drive/papers?section=papers&q=missing+biology+2031", headers=auth_headers(verified_user_token))
    assert response.status_code == 200
    assert response.json() == []
    from models import SearchLog
    db = db_session()
    entry = db.query(SearchLog).filter_by(q="missing biology 2031").one()
    assert entry.results == 0
    assert entry.section == "papers"
    db.close()


def test_ops_omits_metrics_without_a_configured_real_source(client, db_session):
    owner_id, _ = _make_user(db_session, email="owner-ops@gmail.com", role="owner", totp=True)
    response = client.get("/admin/ops", headers=auth_headers(_token(owner_id, db_session, mfa=True)))
    assert response.status_code == 200
    data = response.json()
    assert data["db"]["kind"] == "sqlite"
    assert isinstance(data["errors_today"], int)
    assert "storage" not in data
    assert "backups" not in data
