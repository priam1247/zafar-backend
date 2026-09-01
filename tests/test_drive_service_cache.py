def test_list_files_recursive_uses_cache_within_ttl(monkeypatch):
    import config
    import drive_service

    drive_service.clear_cache()
    monkeypatch.setattr(config.settings, "drive_cache_ttl_seconds", 600)

    calls = {"count": 0}

    def fake_list_children(folder_id):
        calls["count"] += 1
        if folder_id == "root":
            return [{"id": "child-1", "name": "PDF", "mimeType": "application/pdf", "size": "10"}]
        return []

    monkeypatch.setattr(drive_service, "list_children", fake_list_children)

    first = drive_service.list_files_recursive("root")
    second = drive_service.list_files_recursive("root")

    assert first == second
    assert calls["count"] == 1  # second call served from cache, no new Drive API call


def test_list_files_recursive_force_refresh_bypasses_cache(monkeypatch):
    import config
    import drive_service

    drive_service.clear_cache()
    monkeypatch.setattr(config.settings, "drive_cache_ttl_seconds", 600)

    calls = {"count": 0}

    def fake_list_children(folder_id):
        calls["count"] += 1
        return [{"id": "child-1", "name": "PDF", "mimeType": "application/pdf", "size": "10"}]

    monkeypatch.setattr(drive_service, "list_children", fake_list_children)

    drive_service.list_files_recursive("root")
    drive_service.list_files_recursive("root", force_refresh=True)

    assert calls["count"] == 2


def test_clear_cache_forces_a_fresh_walk(monkeypatch):
    import config
    import drive_service

    drive_service.clear_cache()
    monkeypatch.setattr(config.settings, "drive_cache_ttl_seconds", 600)

    calls = {"count": 0}

    def fake_list_children(folder_id):
        calls["count"] += 1
        return []

    monkeypatch.setattr(drive_service, "list_children", fake_list_children)

    drive_service.list_files_recursive("root")
    drive_service.clear_cache()
    drive_service.list_files_recursive("root")

    assert calls["count"] == 2
