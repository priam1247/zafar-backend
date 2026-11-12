import logging
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth import get_current_user, make_signed_download_url
from config import settings
from errors import APIError
from database import get_db
from drive_service import find_child_folder, list_files_recursive, list_children
import drime_service
from rate_limit import limit_request
from models import DownloadLog, User
from routers.auth import _count_today_by_category, _quota_out
from schemas import DownloadUrlOut, PaperOut

router = APIRouter(prefix="/drive", tags=["drive"])
logger = logging.getLogger(__name__)

# Matches the real "Zafar Materials (Sorted)" top-level layout and the
# dashboard sidebar's nav sections. ?section=<key> picks which subtree
# to list.
SECTION_FOLDERS = {
    "papers": "Past Papers",
    "primary": "Primary",
    "books": "Books",
    "notes": "Notes",
    "marking-keys": "Marking Keys",
}

# Primary School is its own browse section but shares the established papers
# allowance; it does not create a second way to spend a new daily quota.
SECTION_QUOTA_CATEGORY = {
    "papers": "paper",
    "primary": "paper",
    "books": "book",
    "notes": "notes",
    "marking-keys": "marking_keys",
}

QUOTA_LIMIT_SETTING = {
    "paper": "daily_paper_limit",
    "book": "daily_book_limit",
    "notes": "daily_notes_limit",
    "marking_keys": "daily_marking_keys_limit",
}

# section -> (resolved_at, folder_id). Uses the same TTL as the Drive tree.
_section_id_cache: dict[str, tuple[float, str]] = {}
_section_lock = threading.Lock()


def _section_folder_id(section: str) -> str:
    folder_name = SECTION_FOLDERS.get(section)
    if folder_name is None:
        raise APIError(status_code=400, detail=f"Unknown section '{section}'", code="section_unknown")

    hit = _section_id_cache.get(section)
    if hit and time.time() - hit[0] < settings.drive_cache_ttl_seconds:
        return hit[1]

    with _section_lock:
        hit = _section_id_cache.get(section)
        if hit and time.time() - hit[0] < settings.drive_cache_ttl_seconds:
            return hit[1]
        folder_id = find_child_folder(settings.drive_root_folder_id, folder_name)
        if folder_id is None:
            raise APIError(
                status_code=404,
                detail="The requested library section is unavailable.",
                code="section_unknown",
            )
        _section_id_cache[section] = (time.time(), folder_id)
        return folder_id


def _require_root():
    if not settings.drive_root_folder_id and not settings.drime_api_key:
        raise APIError(
            status_code=500,
            detail="The library is not configured.",
            code="library_unavailable",
        )


def _list_google_section_files(section: str, force_refresh: bool) -> list[dict]:
    if not settings.drive_root_folder_id:
        return []
    try:
        folder_id = _section_folder_id(section)
    except APIError as exc:
        # A section may exist only in Drime while Google Drive remains
        # configured for the other sections.
        if exc.code == "section_unknown" and settings.drime_api_key:
            return []
        raise
    return list_files_recursive(folder_id, force_refresh=force_refresh)


def _list_section_files(section: str, force_refresh: bool = False) -> list[dict]:
    """List the requested section from each enabled source concurrently."""
    if section not in SECTION_FOLDERS:
        raise APIError(status_code=400, detail=f"Unknown section '{section}'", code="section_unknown")

    # The source adapters maintain independent section-scoped caches. On a
    # cold cache, parallel requests make first-page latency the slower source's
    # latency rather than the sum of Google and Drime latency.
    if settings.drive_root_folder_id and settings.drime_api_key:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="library-source") as pool:
            google_future = pool.submit(_list_google_section_files, section, force_refresh)
            drime_future = pool.submit(drime_service.list_section_files, section, force_refresh)
            return google_future.result() + drime_future.result()

    if settings.drive_root_folder_id:
        return _list_google_section_files(section, force_refresh)
    if settings.drime_api_key:
        return drime_service.list_section_files(section, force_refresh)
    return []


@router.get("/health")
def drive_health(current_user: User = Depends(get_current_user)):
    """
    Confirms the service account can actually reach the destination folder.
    Requires login (like everything else) so this can't be used to probe
    the Drive structure by an outsider.
    """
    _require_root()
    try:
        children = list_children(settings.drive_root_folder_id)
    except Exception as exc:
        logger.exception("Drive listing failed")
        raise APIError(status_code=502, detail="The library could not be reached.", code="drive_unreachable")
    folders = sum(1 for child in children if child.get("mimeType") == "application/vnd.google-apps.folder")
    return {"connected": True, "top_level_items_count": len(children), "folders_count": folders}


@router.get("/papers", response_model=list[PaperOut])
def list_papers(
    request: Request,
    section: str = "papers",
    category: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """
    Lists PDFs for a section. Does NOT return a download URL — the
    frontend must call POST /drive/download/{file_id} when the user
    clicks download. That endpoint is what enforces the quota and
    returns a signed, short-lived URL (a permanent URL here would let
    the quota be bypassed by copying it from the network tab).

    ?category=<name> filters to files whose immediate parent folder name
    matches (case-insensitive). ?q=<text> filters by filename, case-
    insensitive substring match, applied server-side across the selected
    section. `limit`/`offset` support incremental pages; the default is 50.
    """
    limit_request(request, "drive-papers", identifier=str(current_user.id), limit=240, window=60, ip_limit=2000)
    _require_root()
    try:
        files = _list_section_files(section)  # section-scoped, warm caches are fast
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Drive listing failed")
        raise APIError(status_code=502, detail="The library could not be reached.", code="drive_unreachable")

    if category:
        cl = category.strip().lower()
        files = [f for f in files if (f.get("category") or "").strip().lower() == cl]
    if q:
        ql = q.strip().lower()
        files = [f for f in files if ql in f["name"].lower()]

    pdf_files = [f for f in files if f.get("mimeType") == "application/pdf"]
    pdf_files = pdf_files[offset:offset + limit]
    return [
        PaperOut(
            id=f["id"],
            name=f["name"],
            category=f.get("category"),
            size_bytes=int(f["size"]) if f.get("size") else None,
        )
        for f in pdf_files
    ]


@router.get("/categories", response_model=list[str])
def list_categories(
    request: Request,
    section: str = "papers",
    current_user: User = Depends(get_current_user),
):
    """Return the sorted immediate-parent folder names in a section."""
    limit_request(request, "drive-categories", identifier=str(current_user.id), limit=240, window=60, ip_limit=2000)
    _require_root()
    try:
        files = _list_section_files(section)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Drive listing failed")
        raise APIError(status_code=502, detail="The library could not be reached.", code="drive_unreachable")
    return sorted(
        {
            f["category"].strip()
            for f in files
            if f.get("mimeType") == "application/pdf"
            and isinstance(f.get("category"), str)
            and f["category"].strip()
        },
        key=str.casefold,
    )


@router.post("/download/{file_id}", response_model=DownloadUrlOut)
def get_download_url(
    file_id: str,
    request: Request,
    section: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    The ONLY way to get a download URL. Enforces quota server-side, logs
    the download as unconfirmed, and returns a signed URL that dies after
    settings.download_link_ttl_seconds — so links can't be shared or
    reused to bypass the quota. The frontend must call
    POST /drive/confirm/{download_id} once the bytes actually arrive
    (see content.js) — until then the row only counts toward quota for
    as long as the link could still be in flight (see
    _count_today_by_category), so a failed download doesn't permanently
    cost the user a slot.

    Quota enforcement and reservation are one conditional INSERT statement.
    The database atomically evaluates the matching daily/TTL count and inserts
    only while the category remains below its configured limit.
    """
    limit_request(request, "drive-download", limit=30, window=60, ip_limit=2000, identifier=str(current_user.id))
    _require_root()
    quota_category = SECTION_QUOTA_CATEGORY.get(section)
    if quota_category is None:
        raise APIError(status_code=400, detail=f"Unknown section '{section}'", code="section_unknown")

    # Validate against the cached tree first, then refresh once in case a new
    # upload is hidden behind the tree TTL.
    try:
        files = _list_section_files(section)
        meta = next((f for f in files if f["id"] == file_id), None)
        if meta is None:
            files = _list_section_files(section, force_refresh=True)
            meta = next((f for f in files if f["id"] == file_id), None)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Drive listing failed")
        raise APIError(status_code=502, detail="The library could not be reached.", code="drive_unreachable")
    if meta is None:
        raise APIError(status_code=404, detail="File not found in this section", code="file_missing")

    limit = getattr(settings, QUOTA_LIMIT_SETTING[quota_category])
    user_id = current_user.id
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(seconds=settings.download_link_ttl_seconds)
    # Close the authentication/read transaction before SQLite begins its
    # statement-level conditional write; D1 commit is a no-op between requests.
    db.commit()
    inserted = db.execute(
        text(
            """
            INSERT INTO download_logs
                (user_id, file_id, file_name, category, downloaded_at, confirmed)
            SELECT :uid, :fid, :name, :cat, :now, 0
            WHERE (
                SELECT COUNT(*) FROM download_logs
                WHERE user_id = :uid AND category = :cat
                  AND downloaded_at >= :midnight
                  AND (confirmed = TRUE OR downloaded_at >= :cutoff)
            ) < :limit
            """
        ),
        {
            "uid": user_id,
            "fid": file_id,
            "name": meta["name"],
            "cat": quota_category,
            "now": now,
            "midnight": midnight,
            "cutoff": cutoff,
            "limit": limit,
        },
    )
    if inserted.rowcount == 0:
        tomorrow = midnight + timedelta(days=1)
        retry_after = max(1, int((tomorrow - datetime.now(timezone.utc)).total_seconds()))
        raise APIError(
            status_code=429,
            detail="Daily download limit reached",
            code="limit_reached",
            headers={"Retry-After": str(retry_after)},
        )
    download_id = inserted.lastrowid
    db.commit()

    name = meta["name"] if meta["name"].lower().endswith(".pdf") else meta["name"] + ".pdf"
    return DownloadUrlOut(
        download_id=download_id,
        url=make_signed_download_url(
            meta.get("drime_hash", file_id),
            name,
            source=meta.get("source", "google"),
        ),
        expires_in=settings.download_link_ttl_seconds,
        quota=_quota_out(db, user_id),
    )


@router.post("/confirm/{download_id}")
def confirm_download(
    download_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Called by the frontend once the file's bytes have actually been
    received (after the fetch()+blob completes — see content.js). Marks
    the reservation from POST /drive/download as confirmed so it counts
    permanently toward today's quota. Safe to call more than once.
    """
    log = (
        db.query(DownloadLog)
        .filter(DownloadLog.id == download_id, DownloadLog.user_id == current_user.id)
        .first()
    )
    if log is None:
        raise APIError(status_code=404, detail="Download not found", code="file_missing")
    log.confirmed = True
    db.commit()
    return {"status": "confirmed"}
