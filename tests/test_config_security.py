import pytest


def test_allowed_origins_reject_wildcard_and_non_exact_origins(monkeypatch):
    from config import settings

    for invalid in (
        "*",
        "https://*.example.com",
        "http://example.com",
        "https://example.com/path",
        "https://user:pass@example.com",
        "https://example.com?next=other",
    ):
        monkeypatch.setattr(settings, "allowed_origins_csv", invalid)
        with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
            settings.allowed_origins


def test_allowed_origins_permit_production_and_local_development_origins(monkeypatch):
    from config import settings

    monkeypatch.setattr(
        settings,
        "allowed_origins_csv",
        "https://studymw.netlibrary.dpdns.org,http://localhost:5173",
    )
    assert settings.allowed_origins == [
        "https://studymw.netlibrary.dpdns.org",
        "http://localhost:5173",
    ]


def test_api_responses_include_basic_security_headers(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"


def test_required_secret_values_cannot_be_whitespace():
    from config import Settings

    with pytest.raises(ValueError, match="Missing required settings: JWT_SECRET_KEY"):
        Settings(
            jwt_secret_key="   ",
            google_service_account_json="{}",
            download_proxy_base_url="https://downloads.example.test",
            download_signing_secret="test-download-signing-secret",
            smtp_user="test@gmail.com",
            smtp_pass="test-smtp-password",
        )


def test_forwarded_proxy_wildcard_is_rejected():
    from config import Settings

    with pytest.raises(ValueError, match="FORWARDED_ALLOW_IPS"):
        Settings(
            jwt_secret_key="test-secret-do-not-use-in-prod",
            google_service_account_json="{}",
            download_proxy_base_url="https://downloads.example.test",
            download_signing_secret="test-download-signing-secret",
            smtp_user="test@gmail.com",
            smtp_pass="test-smtp-password",
            forwarded_allow_ips="*",
        )
