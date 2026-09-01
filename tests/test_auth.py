from datetime import datetime, timedelta, timezone


def test_register_creates_unverified_user(client, db_session):
    resp = client.post("/auth/register", json={"email": "new@gmail.com", "password": "hunter22"})
    assert resp.status_code == 201
    assert resp.json()["email"] == "new@gmail.com"

    from models import User
    db = db_session()
    user = db.query(User).filter(User.email == "new@gmail.com").first()
    assert user is not None
    assert user.is_verified is False
    assert user.verification_code is not None
    db.close()


def test_register_rejects_non_gmail(client):
    resp = client.post("/auth/register", json={"email": "new@yahoo.com", "password": "hunter22"})
    assert resp.status_code == 422


def test_register_existing_verified_email_rejected(client, db_session):
    client.post("/auth/register", json={"email": "dup@gmail.com", "password": "hunter22"})
    from models import User
    db = db_session()
    user = db.query(User).filter(User.email == "dup@gmail.com").first()
    user.is_verified = True
    db.commit()
    db.close()

    resp = client.post("/auth/register", json={"email": "dup@gmail.com", "password": "other123"})
    assert resp.status_code == 400


def test_register_existing_unverified_email_reissues_code(client, db_session):
    """Re-registering an unverified email should refresh the code, not 400."""
    client.post("/auth/register", json={"email": "retry@gmail.com", "password": "first-pass"})
    resp = client.post("/auth/register", json={"email": "retry@gmail.com", "password": "second-pass"})
    assert resp.status_code == 201


def test_verify_wrong_code_rejected(client):
    client.post("/auth/register", json={"email": "wrongcode@gmail.com", "password": "hunter22"})
    resp = client.post("/auth/verify", json={"email": "wrongcode@gmail.com", "code": "000000"})
    assert resp.status_code == 400


def test_verify_expired_code_does_not_500(client, db_session):
    """
    Regression test for the naive/aware datetime bug: SQLite returns naive
    datetimes, datetime.now(timezone.utc) is aware. Comparing them directly
    used to raise a TypeError (surfacing as a 500), instead of the intended
    400 "expired" response.
    """
    client.post("/auth/register", json={"email": "expired@gmail.com", "password": "hunter22"})

    from models import User
    db = db_session()
    user = db.query(User).filter(User.email == "expired@gmail.com").first()
    code = user.verification_code
    # Simulate what SQLite actually hands back: a naive datetime, in the past.
    user.code_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    db.commit()
    db.close()

    resp = client.post("/auth/verify", json={"email": "expired@gmail.com", "code": code})
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"].lower()


def test_verify_then_login_succeeds(client, db_session):
    client.post("/auth/register", json={"email": "login@gmail.com", "password": "hunter22"})
    from models import User
    db = db_session()
    user = db.query(User).filter(User.email == "login@gmail.com").first()
    code = user.verification_code
    db.close()

    client.post("/auth/verify", json={"email": "login@gmail.com", "code": code})
    resp = client.post("/auth/login", data={"email": "login@gmail.com", "password": "hunter22"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_login_blocked_before_verification(client):
    client.post("/auth/register", json={"email": "unverified@gmail.com", "password": "hunter22"})
    resp = client.post("/auth/login", data={"email": "unverified@gmail.com", "password": "hunter22"})
    assert resp.status_code == 403


def test_login_wrong_password_rejected(client, db_session):
    client.post("/auth/register", json={"email": "wrongpw@gmail.com", "password": "hunter22"})
    from models import User
    db = db_session()
    user = db.query(User).filter(User.email == "wrongpw@gmail.com").first()
    user.is_verified = True
    db.commit()
    db.close()

    resp = client.post("/auth/login", data={"email": "wrongpw@gmail.com", "password": "nope"})
    assert resp.status_code == 401


def test_resend_issues_new_code(client, db_session):
    client.post("/auth/register", json={"email": "resend@gmail.com", "password": "hunter22"})
    resp = client.post("/auth/resend", json={"email": "resend@gmail.com"})
    assert resp.status_code == 200

    from models import User
    db = db_session()
    user = db.query(User).filter(User.email == "resend@gmail.com").first()
    assert user.verification_code is not None
    assert user.code_expires_at is not None
    db.close()


def test_resend_rejected_once_verified(client, db_session):
    client.post("/auth/register", json={"email": "already@gmail.com", "password": "hunter22"})
    from models import User
    db = db_session()
    user = db.query(User).filter(User.email == "already@gmail.com").first()
    user.is_verified = True
    db.commit()
    db.close()

    resp = client.post("/auth/resend", json={"email": "already@gmail.com"})
    assert resp.status_code == 400
