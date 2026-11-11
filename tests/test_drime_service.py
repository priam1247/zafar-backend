from types import SimpleNamespace


def test_drime_api_lists_mapped_sections_and_nested_marking_keys(monkeypatch):
    import drime_service
    from config import settings

    monkeypatch.setattr(settings, "drime_api_key", "test-drime-key")
    monkeypatch.setattr(settings, "drime_api_url", "https://app.drime.cloud/api/v1")
    monkeypatch.setattr(settings, "drime_root_folder_id", "1016158780")
    monkeypatch.setattr(settings, "drime_workspace_id", 0)
    monkeypatch.setattr(settings, "drive_cache_ttl_seconds", 60)
    drime_service.clear_cache()

    root = [
        {"id": 10, "name": "Primary", "type": "folder"},
        {"id": 20, "name": "Notes", "type": "folder"},
        {"id": 30, "name": "Mock", "type": "folder"},
        {"id": 40, "name": "MANEB", "type": "folder"},
        {"id": 50, "name": "Books", "type": "folder"},
    ]
    entries = {
        "1016158780": root,
        "10": [
            {"id": 11, "name": "Primary Paper.pdf", "type": "file", "mime": "application/pdf", "file_size": 1200, "hash": "primary_hash"},
        ],
        "20": [
            {"id": 21, "name": "Study Notes.pdf", "type": "file", "mime": "application/pdf", "file_size": 2300, "hash": "notes_hash"},
        ],
        "30": [
            {"id": 31, "name": "Mock Paper.pdf", "type": "file", "mime": "application/pdf", "file_size": 3400, "hash": "mock_hash"},
        ],
        "40": [
            {"id": 41, "name": "MSCE Paper.pdf", "type": "file", "mime": "application/pdf", "file_size": 4500, "hash": "maneb_hash"},
            {"id": 42, "name": "MANEB Marking Keys", "type": "folder"},
        ],
        "42": [
            {"id": 43, "name": "Answer Key.pdf", "type": "file", "mime": "application/pdf", "file_size": 5600, "hash": "marking_hash"},
        ],
        "50": [
            {"id": 51, "name": "Chemistry Book.pdf", "type": "file", "mime": "application/pdf", "file_size": 6700, "hash": "book_hash"},
        ],
    }
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append((url, headers, params, timeout))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"data": entries.get(str(params["folderId"]), [])},
        )

    monkeypatch.setattr(drime_service.requests, "get", fake_get)

    papers = drime_service.list_section_files("papers")
    notes = drime_service.list_section_files("notes")
    books = drime_service.list_section_files("books")
    marking = drime_service.list_section_files("marking-keys")

    assert {item["drime_hash"] for item in papers} == {"primary_hash", "mock_hash", "maneb_hash"}
    assert {item["drime_hash"] for item in notes} == {"notes_hash"}
    assert {item["drime_hash"] for item in books} == {"book_hash"}
    assert {item["drime_hash"] for item in marking} == {"marking_hash"}
    assert all(item["id"].startswith("drime~") for item in papers + notes + books + marking)
    assert all(call[1]["Authorization"] == "Bearer test-drime-key" for call in calls)
    assert all(call[2]["workspaceId"] == 0 for call in calls)
    drime_service.clear_cache()


def test_drime_list_items_share_existing_papers_quota_and_signed_flow(
    client, fake_drive_tree, verified_user_token, monkeypatch, db_session
):
    import routers.drive as drive_router
    from config import settings
    from models import DownloadLog

    monkeypatch.setattr(settings, "drime_api_key", "test-drime-key")
    monkeypatch.setattr(drive_router.drime_service, "list_section_files", lambda *_a, **_kw: [{
        "id": "drime~drime-paper-hash",
        "drime_hash": "drime-paper-hash",
        "source": "drime",
        "name": "Drime Past Paper.pdf",
        "mimeType": "application/pdf",
        "size": "1234",
        "category": "MANEB",
    }])

    listed = client.get("/drive/papers?section=papers", headers={"Authorization": f"Bearer {verified_user_token}"})
    assert listed.status_code == 200
    assert {"file-1", "drime~drime-paper-hash"}.issubset({item["id"] for item in listed.json()})

    downloaded = client.post(
        "/drive/download/drime~drime-paper-hash?section=papers",
        headers={"Authorization": f"Bearer {verified_user_token}"},
    )
    assert downloaded.status_code == 200
    assert "/dl/drime/drime-paper-hash?" in downloaded.json()["url"]
    assert downloaded.json()["quota"]["papers_downloaded_today"] == 1

    from database import SessionLocal
    db = SessionLocal()
    row = db.query(DownloadLog).filter_by(file_id="drime~drime-paper-hash").one()
    assert row.category == "paper"
    db.close()
