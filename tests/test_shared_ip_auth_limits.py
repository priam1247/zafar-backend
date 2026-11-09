from fastapi import HTTPException
from starlette.requests import Request

from rate_limit import limit_request


def same_socket_request():
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/auth/register",
        "headers": [],
        "client": ("school-nat", 42000),
        "server": ("api", 443),
        "scheme": "https",
        "query_string": b"",
    })


def test_email_account_limits_do_not_block_other_students_on_same_ip():
    request = same_socket_request()
    scope = "shared-ip-auth-regression"

    # Each student has an independent per-email bucket even though their
    # requests arrive from the same school NAT and the IP ceiling is disabled.
    limit_request(request, scope, "student-one@example.test", limit=1, window=60, ip_limit=0)
    limit_request(request, scope, "student-two@example.test", limit=1, window=60, ip_limit=0)

    # The individual account still has its own limit.
    try:
        limit_request(request, scope, "student-one@example.test", limit=1, window=60, ip_limit=0)
    except HTTPException as exc:
        assert exc.status_code == 429
    else:
        raise AssertionError("the first student's own account limit should still apply")


def test_all_auth_routes_disable_shared_ip_buckets(client, monkeypatch):
    import routers.auth as auth_router

    observed = []

    def record(_request, _scope, _identifier="", **kwargs):
        observed.append(kwargs.get("ip_limit"))

    monkeypatch.setattr(auth_router, "limit_request", record)

    registered = client.post("/auth/register", json={
        "name": "Shared IP Student",
        "email": "shared-ip-student@gmail.com",
        "password": "student-pass-1",
    })
    assert registered.status_code == 201

    from database import SessionLocal
    from models import User

    db = SessionLocal()
    user = db.query(User).filter_by(email="shared-ip-student@gmail.com").first()
    db.close()

    assert client.post("/auth/resend", json={"email": "shared-ip-student@gmail.com"}).status_code == 200
    db = SessionLocal()
    user = db.query(User).filter_by(email="shared-ip-student@gmail.com").first()
    verification_code = user.verification_code
    db.close()

    assert client.post("/auth/verify", json={
        "email": "shared-ip-student@gmail.com", "code": verification_code,
    }).status_code == 200
    assert client.post("/auth/login", data={
        "email": "shared-ip-student@gmail.com", "password": "student-pass-1",
    }).status_code == 200
    assert client.post("/auth/forgot-password", json={
        "email": "shared-ip-student@gmail.com",
    }).status_code == 200

    db = SessionLocal()
    user = db.query(User).filter_by(email="shared-ip-student@gmail.com").first()
    reset_code = user.reset_code
    db.close()
    assert client.post("/auth/reset-password", json={
        "email": "shared-ip-student@gmail.com",
        "code": reset_code,
        "new_password": "student-pass-2",
    }).status_code == 200

    assert observed == [0, 0, 0, 0, 0, 0]
