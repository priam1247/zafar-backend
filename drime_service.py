"""Read-only adapter for the Drime Cloud API used by the library.

The API contract here follows the working Drime client flow: Bearer API key,
GET /drive/file-entries with folderId/perPage/page/workspaceId, and file hashes
for downloads through the signed download Worker. The API key is never sent to
browser clients.
"""
from __future__ import annotations

import logging
import mimetypes
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from config import settings

logger = logging.getLogger(__name__)
_PAGE_SIZE = 100
_MAX_PAGES = 1000
_FOLDER_MIME = "application/vnd.google-apps.folder"
_MARKING_PATH = re.compile(r"\bmark(?:ing)?\s*(?:keys?|schemes?)\b|\bmark\s*schemes?\b", re.I)

# (root id, section) -> (cached timestamp, files for that selected section).
_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def _folder_names(section: str) -> set[str]:
    field = {
        "papers": "drime_papers_folder_names",
        "primary": "drime_primary_folder_names",
        "books": "drime_books_folder_names",
        "notes": "drime_notes_folder_names",
        "marking-keys": "drime_marking_keys_folder_names",
    }.get(section)
    if field is None:
        return set()
    return {
        name.strip().casefold()
        for name in getattr(settings, field).split(",")
        if name.strip()
    }


def _entry_list(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize common Laravel-style Drime list response envelopes."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)], {}
    if not isinstance(payload, dict):
        raise RuntimeError("Drime API returned an unexpected listing response")

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    data = payload.get("data", payload.get("entries", payload.get("files", [])))
    if isinstance(data, dict):
        meta = data.get("meta", meta) if isinstance(data.get("meta", meta), dict) else meta
        data = data.get("data", data.get("entries", data.get("files", [])))
    if not isinstance(data, list):
        raise RuntimeError("Drime API response did not contain a file list")
    return [row for row in data if isinstance(row, dict)], meta


def _api_base_url() -> str:
    parsed = urlsplit(settings.drime_api_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "app.drime.cloud"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise RuntimeError("Drime API URL must use the official HTTPS endpoint")
    return settings.drime_api_url.rstrip("/")


def _list_children(folder_id: str) -> list[dict[str, Any]]:
    if not settings.drime_api_key:
        return []

    url = f"{_api_base_url()}/drive/file-entries"
    headers = {
        "Authorization": f"Bearer {settings.drime_api_key}",
        "Accept": "application/json",
    }
    entries: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        try:
            response = requests.get(
                url,
                headers=headers,
                params={
                    "folderId": folder_id,
                    "workspaceId": settings.drime_workspace_id,
                    "perPage": _PAGE_SIZE,
                    "page": page,
                },
                timeout=(10, 45),
            )
        except requests.RequestException as exc:
            raise RuntimeError("Drime API could not be reached") from exc

        if response.status_code in (401, 403):
            raise RuntimeError("Drime API credentials were rejected")
        if response.status_code == 429:
            raise RuntimeError("Drime API rate limit reached")
        if response.status_code != 200:
            raise RuntimeError(f"Drime API returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Drime API returned invalid JSON") from exc

        page_entries, meta = _entry_list(payload)
        entries.extend(page_entries)
        last_page = meta.get("last_page") or meta.get("lastPage")
        if last_page is not None:
            if page >= int(last_page):
                return entries
        elif len(page_entries) < _PAGE_SIZE:
            return entries

    raise RuntimeError("Drime listing exceeded the pagination safety limit")


def _is_folder(entry: dict[str, Any]) -> bool:
    return (
        entry.get("is_folder") is True
        or entry.get("isFolder") is True
        or str(entry.get("type", entry.get("file_type", ""))).casefold() in {"folder", "directory"}
        or (entry.get("mime") or entry.get("mimeType") or entry.get("mime_type")) == _FOLDER_MIME
    )


def _normalise_file(entry: dict[str, Any], category: str) -> dict[str, Any] | None:
    file_hash = entry.get("hash") or entry.get("file_hash") or entry.get("download_hash")
    if not isinstance(file_hash, str) or not file_hash:
        logger.warning("Skipping Drime file entry without a download hash")
        return None

    name = entry.get("name") or entry.get("file_name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    extension = str(entry.get("extension") or "").casefold().lstrip(".")
    mime_type = entry.get("mime") or entry.get("mimeType") or entry.get("mime_type") or mimetypes.guess_type(name)[0]
    if not mime_type and extension == "pdf":
        mime_type = "application/pdf"
    mime_type = mime_type or "application/octet-stream"
    size = entry.get("file_size")
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None

    return {
        # `~` cannot occur in a Google Drive file id, so this distinguishes
        # provider ids without changing the existing PaperOut response shape.
        "id": f"drime~{file_hash}",
        "drime_hash": file_hash,
        "source": "drime",
        "name": name,
        "mimeType": mime_type,
        "size": str(size) if size is not None else None,
        "category": category,
    }


def _walk(folder_id: str, folder_name: str, path: tuple[str, ...], depth: int, output: list[dict[str, Any]]) -> None:
    if depth > settings.drive_max_depth:
        logger.warning(
            "Drime tree depth limit reached at folder id %s (depth %s, max %s)",
            folder_id,
            depth,
            settings.drive_max_depth,
        )
        return
    for entry in _list_children(folder_id):
        name = str(entry.get("name") or entry.get("file_name") or "").strip()
        if not name:
            continue
        if _is_folder(entry):
            child_id = entry.get("id")
            if child_id is not None:
                _walk(str(child_id), name, (*path, name), depth + 1, output)
        else:
            normalized = _normalise_file(entry, path[-1] if path else folder_name)
            if normalized:
                normalized["drime_path"] = path
                output.append(normalized)


def _build_section_files(root_id: str, section: str) -> list[dict[str, Any]]:
    """Walk only the Drime root folders belonging to the requested section."""
    section_names = {
        name: _folder_names(name)
        for name in ("papers", "primary", "books", "notes", "marking-keys")
    }
    # Older deployments may still explicitly configure Primary in the papers
    # CSV. Give the dedicated school section precedence to prevent duplicates.
    section_names["papers"].difference_update(section_names["primary"])

    if section == "marking-keys":
        # Marking folders may be nested below a MANEB/Mock folder, or directly
        # below the Drime root. Inspect only those known paper roots plus any
        # directly named marking-key roots, not Books, Notes, or Primary.
        candidate_names = set(section_names["marking-keys"])
        candidate_names.update(section_names["papers"])
    else:
        candidate_names = section_names[section]

    results: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for root_entry in _list_children(root_id):
        if not _is_folder(root_entry):
            continue
        name = str(root_entry.get("name") or "").strip()
        folder_id = root_entry.get("id")
        if not name or folder_id is None or name.casefold() not in candidate_names:
            continue

        collected: list[dict[str, Any]] = []
        _walk(str(folder_id), name, (name,), 1, collected)
        for file in collected:
            path = " / ".join(file.get("drime_path", ()))
            is_marking_key = bool(_MARKING_PATH.search(path))
            if section == "marking-keys":
                if not is_marking_key:
                    continue
            elif is_marking_key:
                continue

            file_hash = file["drime_hash"]
            if file_hash not in seen_hashes:
                seen_hashes.add(file_hash)
                results.append(file)
    return results


def list_section_files(section: str, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return Drime PDFs' containing files for one of the library sections."""
    if not settings.drime_api_key:
        return []
    if section not in {"papers", "primary", "books", "notes", "marking-keys"}:
        return []

    root_id = str(settings.drime_root_folder_id)
    cache_key = (root_id, section)
    ttl = settings.drive_cache_ttl_seconds
    now = time.monotonic()
    hit = _cache.get(cache_key)
    if not force_refresh and ttl > 0 and hit and now - hit[0] < ttl:
        return list(hit[1])

    with _cache_lock:
        hit = _cache.get(cache_key)
        now = time.monotonic()
        if not force_refresh and ttl > 0 and hit and now - hit[0] < ttl:
            return list(hit[1])
        files = _build_section_files(root_id, section)
        _cache[cache_key] = (time.monotonic(), files)
        return list(files)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
