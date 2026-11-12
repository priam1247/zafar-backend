from types import SimpleNamespace


def test_drime_api_lists_mapped_sections_and_nested_marking_keys(monkeypatch):
    import drime_service
    from config import settings

    monkeypatch.setattr(settings, "drime_api_key", "test-drime-key")
    monkeypatch.setattr(settings, "drime_api_url", "https://app.drime.cloud/api/v1")
    # Simulate an existing deployment still carrying the pre-split setting.
    monkeypatch.setattr(settings, "drime_papers_folder_names", "Primary,Mock,MANEB")
    monkeypatch.setattr(settings, "drime_primary_folder_names", "Primary")
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
    papers_folder_ids = {str(call[2]["folderId"]) for call in calls}
    assert papers_folder_ids == {"1016158780", "30", "40", "42"}
    paper_call_count = len(calls)

    primary = drime_service.list_section_files("primary")
    primary_folder_ids = {str(call[2]["folderId"]) for call in calls[paper_call_count:]}
    assert primary_folder_ids == {"1016158780", "10"}
    notes = drime_service.list_section_files("notes")
    books = drime_service.list_section_files("books")
    marking = drime_service.list_section_files("marking-keys")

    assert {item["drime_hash"] for item in papers} == {"mock_hash", "maneb_hash"}
    assert {item["drime_hash"] for item in primary} == {"primary_hash"}
    assert {item["drime_hash"] for item in notes} == {"notes_hash"}
    assert {item["drime_hash"] for item in books} == {"book_hash"}
    assert {item["drime_hash"] for item in marking} == {"marking_hash"}
    assert all(item["id"].startswith("drime~") for item in papers + primary + notes + books + marking)
    assert all(call[1]["Authorization"] == "Bearer test-drime-key" for call in calls)
    assert all(call[2]["workspaceId"] == 0 for call in calls)
    drime_service.clear_cache()


def test_primary_drime_listing_shares_papers_quota_and_signed_flow(
    client, verified_user_token, monkeypatch
):
    import routers.drive as drive_router
    from config import settings
    from database import SessionLocal
    from models import DownloadLog

    monkeypatch.setattr(settings, "drime_api_key", "test-drime-key")
    monkeypatch.setattr(settings, "drive_root_folder_id", "")
    primary_file = {
        "id": "drime~primary-paper-hash",
        "drime_hash": "primary-paper-hash",
        "source": "drime",
        "name": "Primary School Paper.pdf",
        "mimeType": "application/pdf",
        "size": "1234",
        "category": "Standard 8",
    }
    monkeypatch.setattr(
        drive_router.drime_service,
        "list_section_files",
        lambda section, _force_refresh=False: [primary_file] if section == "primary" else [],
    )
    headers = {"Authorization": f"Bearer {verified_user_token}"}

    listed = client.get("/drive/papers?section=primary", headers=headers)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == ["drime~primary-paper-hash"]

    downloaded = client.post(
        "/drive/download/drime~primary-paper-hash?section=primary",
        headers=headers,
    )
    assert downloaded.status_code == 200
    assert "/dl/drime/primary-paper-hash?" in downloaded.json()["url"]
    assert downloaded.json()["quota"]["papers_downloaded_today"] == 1

    db = SessionLocal()
    try:
        row = db.query(DownloadLog).filter_by(file_id="drime~primary-paper-hash").one()
        assert row.category == "paper"
    finally:
        db.close()


def test_google_and_drime_section_scans_run_in_parallel(monkeypatch):
    import threading

    import routers.drive as drive_router
    from config import settings

    monkeypatch.setattr(settings, "drive_root_folder_id", "google-root")
    monkeypatch.setattr(settings, "drime_api_key", "test-drime-key")
    monkeypatch.setattr(drive_router, "_section_folder_id", lambda _section: "google-section")
    barrier = threading.Barrier(2)

    def google_list(_folder_id, force_refresh=False):
        barrier.wait(timeout=2)
        return [{"id": "google-file"}]

    def drime_list(_section, force_refresh=False):
        barrier.wait(timeout=2)
        return [{"id": "drime-file"}]

    monkeypatch.setattr(drive_router, "list_files_recursive", google_list)
    monkeypatch.setattr(drive_router.drime_service, "list_section_files", drime_list)

    result = drive_router._list_section_files("papers")
    assert {item["id"] for item in result} == {"google-file", "drime-file"}
