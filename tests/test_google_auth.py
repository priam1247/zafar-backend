def _enable_google(monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "google_client_id", "test-client.apps.googleusercontent.com")


def test_google_login_disabled_without_client_id(client, monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "google_client_id", "")
    response = client.post("/auth/google", json={"credential": "test-id-token"})
    assert response.status_code == 503
    assert response.headers["x-error-code"] == "google_not_configured"


def test_google_login_verifies_token_and_creates_verified_account(client, db_session, monkeypatch):
    _enable_google(monkeypatch)
    import routers.auth as auth_router
    from models import User

    def verify(token, request, audience):
        assert token == "signed-google-id-token"
        assert audience == "test-client.apps.googleusercontent.com"
        return {
            "sub": "google-subject-123",
            "email": "Student.Name@gmail.com",
            "email_verified": True,
            "name": "Google Student",
        }

    monkeypatch.setattr(auth_router.google_id_token, "verify_oauth2_token", verify)
    response = client.post("/auth/google", json={"credential": "signed-google-id-token"})
    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"

    db = db_session()
    user = db.query(User).filter_by(email="student.name@gmail.com").one()
    assert user.is_verified is True
    assert user.name == "Google Student"
    assert user.username
    db.close()

    profile = client.get("/auth/me", headers={"Authorization": f"Bearer {response.json()['access_token']}"})
    assert profile.status_code == 200
    assert profile.json()["email"] == "student.name@gmail.com"


def test_google_login_rejects_unverified_or_non_gmail_identity(client, monkeypatch):
    _enable_google(monkeypatch)
    import routers.auth as auth_router

    monkeypatch.setattr(
        auth_router.google_id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: {
            "sub": "subject", "email": "student@gmail.com", "email_verified": False,
        },
    )
    unverified = client.post("/auth/google", json={"credential": "valid-signature"})
    assert unverified.status_code == 401
    assert unverified.headers["x-error-code"] == "google_invalid_token"

    monkeypatch.setattr(
        auth_router.google_id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: {
            "sub": "subject", "email": "student@example.org", "email_verified": True,
        },
    )
    unsupported = client.post("/auth/google", json={"credential": "valid-signature"})
    assert unsupported.status_code == 403
    assert unsupported.headers["x-error-code"] == "google_email_unsupported"


def test_google_login_verifier_failure_is_generic(client, monkeypatch):
    _enable_google(monkeypatch)
    import routers.auth as auth_router

    def reject(*args, **kwargs):
        raise ValueError("detailed provider validation error must not leak")

    monkeypatch.setattr(auth_router.google_id_token, "verify_oauth2_token", reject)
    response = client.post("/auth/google", json={"credential": "invalid-token"})
    assert response.status_code == 401
    assert response.headers["x-error-code"] == "google_invalid_token"
    assert "detailed provider" not in response.text


def test_google_login_verifies_existing_unverified_account(client, db_session, monkeypatch):
    _enable_google(monkeypatch)
    import routers.auth as auth_router
    from models import User

    client.post("/auth/register", json={"name": "Existing Student", "email": "existing@gmail.com", "password": "hunter22"})
    monkeypatch.setattr(
        auth_router.google_id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: {
            "sub": "subject", "email": "existing@gmail.com", "email_verified": True,
        },
    )
    response = client.post("/auth/google", json={"credential": "verified-token"})
    assert response.status_code == 200

    db = db_session()
    user = db.query(User).filter_by(email="existing@gmail.com").one()
    assert user.is_verified is True
    assert user.verification_code is None
    db.close()
