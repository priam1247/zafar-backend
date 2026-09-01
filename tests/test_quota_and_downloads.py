def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_list_papers_requires_auth(client, fake_drive_tree):
    resp = client.get("/drive/papers")
    assert resp.status_code == 401


def test_list_papers_omits_download_url(client, fake_drive_tree, verified_user_token):
    """
    /drive/papers must never hand out a direct download URL — that was
    the quota-bypass hole the signed-URL redesign closes. Only
    /drive/download/{id} should mint a URL.
    """
    resp = client.get("/drive/papers", headers=_auth_headers(verified_user_token))
    assert resp.status_code == 200
    for paper in resp.json():
        assert "download_url" not in paper
        assert "url" not in paper


def test_download_enforces_quota(client, fake_drive_tree, verified_user_token, db_session):
    """DAILY_PAPER_LIMIT is set to 3 in tests/conftest.py."""
    headers = _auth_headers(verified_user_token)

    for _ in range(3):
        resp = client.post("/drive/download/file-1?section=papers", headers=headers)
        assert resp.status_code == 200
        assert "url" in resp.json()

    # 4th request for the same category should be blocked.
    resp = client.post("/drive/download/file-1?section=papers", headers=headers)
    assert resp.status_code == 429


def test_download_logs_row_per_download(client, fake_drive_tree, verified_user_token, db_session):
    headers = _auth_headers(verified_user_token)
    client.post("/drive/download/file-1?section=papers", headers=headers)

    from models import DownloadLog
    db = db_session()
    count = db.query(DownloadLog).count()
    db.close()
    assert count == 1


def test_download_unknown_file_id_404s(client, fake_drive_tree, verified_user_token):
    resp = client.post("/drive/download/does-not-exist?section=papers", headers=_auth_headers(verified_user_token))
    assert resp.status_code == 404


def test_book_and_paper_quotas_are_independent(client, fake_drive_tree, verified_user_token):
    """DAILY_BOOK_LIMIT is 2 in tests/conftest.py, separate from papers."""
    headers = _auth_headers(verified_user_token)

    resp = client.post("/drive/download/file-1?section=papers", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["quota"]["papers_downloaded_today"] == 1
    assert resp.json()["quota"]["books_downloaded_today"] == 0


def test_make_signed_download_url_is_verifiable(monkeypatch):
    import base64
    import hashlib
    import hmac

    import config
    monkeypatch.setattr(config.settings, "download_proxy_base_url", "https://zafar-dl.example.workers.dev")
    monkeypatch.setattr(config.settings, "download_signing_secret", "test-signing-secret")
    monkeypatch.setattr(config.settings, "download_link_ttl_seconds", 300)

    from auth import make_signed_download_url
    url = make_signed_download_url("file-123", "Some Paper.pdf")

    assert url.startswith("https://zafar-dl.example.workers.dev/dl/file-123?")
    params = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    assert "exp" in params and "sig" in params

    msg = f"file-123.{params['exp']}".encode()
    expected_sig = hmac.new(b"test-signing-secret", msg, hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected_sig).decode().rstrip("=")
    assert params["sig"] == expected_b64


def test_make_signed_download_url_falls_back_without_secret(monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "download_proxy_base_url", "")
    monkeypatch.setattr(config.settings, "download_signing_secret", "")

    from auth import make_signed_download_url
    url = make_signed_download_url("file-123", "Some Paper.pdf")
    assert url == "https://drive.google.com/uc?export=download&id=file-123"
