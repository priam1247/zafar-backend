"""
Google Drive wrapper. Heavy Google libs are imported lazily (first Drive
call), and the recursive folder walk is cached in-process with a TTL —
the two biggest RAM/latency wins on the free tier.
"""

import threading
import time
from functools import lru_cache

from config import settings

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

_tree_cache: dict[str, tuple[float, list]] = {}
_tree_lock = threading.Lock()


@lru_cache
def get_drive_service():
    # Lazy import: googleapiclient + google-auth cost ~30-50MB RSS.
    # Don't pay it at boot — only on the first Drive request.
    import json

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if not settings.google_service_account_json:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not set — paste the full "
            "service_account.json content into that env var on Koyeb."
        )
    try:
        info = json.loads(settings.google_service_account_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}"
        ) from exc

    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _list_all(query: str, fields: str):
    service = get_drive_service()
    files, page_token = [], None
    while True:
        response = (
            service.files()
            .list(q=query, fields=f"nextPageToken, {fields}",
                  pageSize=1000, pageToken=page_token)  # max page size = fewest round-trips
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return files


def list_children(folder_id: str):
    """Returns immediate children (files + folders) of a given folder ID."""
    return _list_all(
        query=f"'{folder_id}' in parents and trashed = false",
        fields="files(id, name, mimeType, size)",
    )


def find_child_folder(parent_id: str, name: str):
    """
    Finds an immediate subfolder of parent_id matching `name`
    (case-insensitive). Returns its id, or None if not found.
    """
    target = name.strip().lower()
    for item in list_children(parent_id):
        if (item["mimeType"] == "application/vnd.google-apps.folder"
                and item["name"].strip().lower() == target):
            return item["id"]
    return None


def list_files_recursive(folder_id: str, max_depth: int = 6, force_refresh: bool = False):
    """
    Walks the folder tree under folder_id and returns every non-folder
    file found, cached in-process for settings.drive_cache_ttl_seconds.
    Each file is tagged with "category" = the name of its immediate
    parent folder (e.g. "MSCE Maneb", "Junior").
    """
    ttl = settings.drive_cache_ttl_seconds
    if not force_refresh and ttl > 0:
        hit = _tree_cache.get(folder_id)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]

    with _tree_lock:  # one thread rebuilds; concurrent requests wait then hit cache
        hit = _tree_cache.get(folder_id)
        if not force_refresh and ttl > 0 and hit and time.time() - hit[0] < ttl:
            return hit[1]

        results = []

        def _walk(fid, folder_name, depth):
            if depth > max_depth:
                return
            for item in list_children(fid):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    _walk(item["id"], item["name"], depth + 1)
                else:
                    item["category"] = folder_name
                    results.append(item)

        _walk(folder_id, None, 0)
        _tree_cache[folder_id] = (time.time(), results)
        return results


def find_cached_file(root_folder_id: str, file_id: str):
    """O(n) scan of the cached tree — avoids a Drive API call per download."""
    for f in list_files_recursive(root_folder_id):
        if f["id"] == file_id:
            return f
    return None


def clear_cache():
    with _tree_lock:
        _tree_cache.clear()


def get_file_metadata(file_id: str):
    return get_drive_service().files().get(
        fileId=file_id, fields="id, name, mimeType, size"
    ).execute()
