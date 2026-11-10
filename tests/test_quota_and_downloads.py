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


def test_make_signed_download_url_fails_without_signing_config(monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "download_proxy_base_url", "")
    monkeypatch.setattr(config.settings, "download_signing_secret", "")

    import pytest
    from fastapi import HTTPException
    from auth import make_signed_download_url
    with pytest.raises(HTTPException, match="Download signing is not configured"):
        make_signed_download_url("file-123", "Some Paper.pdf")


def test_categories_endpoint_returns_distinct_sorted_categories(client, fake_drive_tree, verified_user_token):
    response = client.get("/drive/categories?section=papers", headers=_auth_headers(verified_user_token))
    assert response.status_code == 200
    assert response.json() == ["Books", "MSCE Maneb"]
    assert response.headers["cache-control"] == "no-store"


def test_download_force_refreshes_stale_tree_before_404(client, monkeypatch, verified_user_token):
    import routers.drive as drive_router

    calls = []
    fresh_file = {"id": "fresh-file", "name": "Fresh.pdf", "mimeType": "application/pdf", "size": "42", "category": "New"}

    def list_files(_folder_id, max_depth=6, force_refresh=False):
        calls.append(force_refresh)
        return [fresh_file] if force_refresh else []

    monkeypatch.setattr(drive_router, "list_files_recursive", list_files)
    monkeypatch.setattr(drive_router, "_section_folder_id", lambda section: "resolved-folder-id")
    response = client.post(
        "/drive/download/fresh-file?section=papers",
        headers=_auth_headers(verified_user_token),
    )
    assert response.status_code == 200
    assert calls == [False, True]


def test_drive_refresh_route_is_removed(client, verified_user_token):
    response = client.post("/drive/refresh", headers=_auth_headers(verified_user_token))
    assert response.status_code == 404


def test_api_cors_allows_only_new_production_origin(client):
    new_origin = "https://studymw.netlibrary.dpdns.org"
    allowed = client.options(
        "/auth/login",
        headers={
            "Origin": new_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == new_origin

    for old_origin in (
        "https://library.zafarh.dpdns.org",
        "http://studymw.netlibrary.dpdns.org",
    ):
        denied = client.get("/health", headers={"Origin": old_origin})
        assert "access-control-allow-origin" not in denied.headers


def test_each_section_has_an_independent_daily_quota(client, fake_drive_tree, verified_user_token):
    headers = _auth_headers(verified_user_token)
    sections_and_limits = {
        "papers": 3,
        "books": 2,
        "notes": 3,
        "marking-keys": 10,
    }

    for section, limit in sections_and_limits.items():
        for _ in range(limit):
            response = client.post(f"/drive/download/file-1?section={section}", headers=headers)
            assert response.status_code == 200, (section, response.text)
        blocked = client.post(f"/drive/download/file-1?section={section}", headers=headers)
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"]

    quota_response = client.get("/auth/quota", headers=headers)
    assert quota_response.status_code == 200
    quota = quota_response.json()
    assert quota["papers_downloaded_today"] == 3
    assert quota["papers_limit"] == 3
    assert quota["books_downloaded_today"] == 2
    assert quota["books_limit"] == 2
    assert quota["notes_downloaded_today"] == 3
    assert quota["notes_limit"] == 3
    assert quota["marking_keys_downloaded_today"] == 10
    assert quota["marking_keys_limit"] == 10
    assert quota["papers_left"] == quota["books_left"] == 0
    assert quota["notes_left"] == quota["marking_keys_left"] == 0


def test_download_without_section_is_refused(client, fake_drive_tree, verified_user_token):
    response = client.post(
        "/drive/download/file-1",
        headers=_auth_headers(verified_user_token),
    )
    assert response.status_code == 422


def test_a_note_cannot_be_downloaded_as_a_paper(client, monkeypatch, verified_user_token):
    """A file outside the requested section is refused without spending quota."""
    import routers.drive as drive_router

    papers = [{
        "id": "file-1",
        "name": "Paper One.pdf",
        "mimeType": "application/pdf",
        "size": "1000",
        "category": "MSCE Maneb",
    }]
    notes = [{
        "id": "note-1",
        "name": "Note One.pdf",
        "mimeType": "application/pdf",
        "size": "1000",
        "category": "Form 1",
    }]

    monkeypatch.setattr(drive_router, "_section_folder_id", lambda section: section)
    monkeypatch.setattr(
        drive_router,
        "list_files_recursive",
        lambda folder_id, max_depth=6, force_refresh=False: notes if folder_id == "notes" else papers,
    )

    headers = _auth_headers(verified_user_token)
    refused = client.post("/drive/download/note-1?section=papers", headers=headers)
    assert refused.status_code == 404

    response = client.post("/drive/download/file-1?section=papers", headers=headers)
    assert response.status_code == 200
    assert response.json()["quota"]["papers_downloaded_today"] == 1
