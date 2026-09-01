import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_user, make_signed_download_url
from config import settings
from database import get_db
from drive_service import clear_cache, find_child_folder, list_files_recursive, list_children
from models import DownloadLog, User
from routers.auth import _count_today_by_category, _quota_out
from schemas import DownloadUrlOut, PaperOut

router = APIRouter(prefix="/drive", tags=["drive"])

# Matches the real "Zafar Materials (Sorted)" top-level layout and the
# dashboard sidebar's nav sections. ?section=<key> picks which subtree
# to list.
SECTION_FOLDERS = {
    "papers": "Past Papers",
    "books": "Books",
    "notes": "Notes",
    "marking-keys": "Marking Keys",
}

# Which quota bucket each section draws from.
SECTION_QUOTA_CATEGORY = {
    "papers": "paper",
    "books": "book",
    "notes": "paper",
    "marking-keys": "paper",
}

# section -> (resolved_at, folder_id). Saves one Drive API round-trip on
# every /papers request; folder IDs basically never change.
_section_id_cache: dict[str, tuple[float, str]] = {}
_section_lock = threading.Lock()


def _section_folder_id(section: str) -> str:
    folder_name = SECTION_FOLDERS.get(section)
    if folder_name is None:
        raise HTTPException(status_code=400, detail=f"Unknown section '{section}'")

    hit = _section_id_cache.get(section)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]

    with _section_lock:
        hit = _section_id_cache.get(section)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        folder_id = find_child_folder(settings.drive_root_folder_id, folder_name)
        if folder_id is None:
            raise HTTPException(
                status_code=404,
                detail=f"'{folder_name}' folder not found directly under the Drive root",
            )
        _section_id_cache[section] = (time.time(), folder_id)
        return folder_id


def _require_root():
    if not settings.drive_root_folder_id:
        raise HTTPException(
            status_code=500,
            detail="DRIVE_ROOT_FOLDER_ID is not set yet — fill it in once "
            "the Apps Script sorter has finished building Zafar Materials (Sorted).",
        )


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
        raise HTTPException(status_code=502, detail=f"Could not reach Drive: {exc}")
    return {
        "connected": True,
        "root_folder_id": settings.drive_root_folder_id,
        "top_level_items": [c["name"] for c in children],
    }


@router.post("/refresh")
def refresh(current_user: User = Depends(get_current_user)):
    """Call after uploading new material to Drive to bust the caches."""
    clear_cache()
    with _section_lock:
        _section_id_cache.clear()
    return {"status": "refreshed"}


@router.get("/papers", response_model=list[PaperOut])
def list_papers(
    section: str = "papers",
    category: str | None = None,
    q: str | None = None,
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
    insensitive substring match, applied server-side across the section.
    """
    _require_root()
    try:
        files = list_files_recursive(_section_folder_id(section))  # cached: ~1ms warm
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Drive: {exc}")

    if category:
        cl = category.strip().lower()
        files = [f for f in files if (f.get("category") or "").strip().lower() == cl]
    if q:
        ql = q.strip().lower()
        files = [f for f in files if ql in f["name"].lower()]

    return [
        PaperOut(
            id=f["id"],
            name=f["name"],
            category=f.get("category"),
            size_bytes=int(f["size"]) if f.get("size") else None,
        )
        for f in files
        if f.get("mimeType") == "application/pdf"
    ]


@router.post("/download/{file_id}", response_model=DownloadUrlOut)
def get_download_url(
    file_id: str,
    section: str = "papers",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    The ONLY way to get a download URL. Enforces quota server-side, logs
    the download, and returns a signed URL that dies after
    settings.download_link_ttl_seconds — so links can't be shared or
    reused to bypass the quota.

    NOTE: the quota check-then-insert below is not fully race-proof under
    concurrent requests from the same user (two near-simultaneous calls
    can both pass the check before either commits). Acceptable for a
    single-process hobby deployment; see the security checklist if you
    want a stricter guarantee.
    """
    _require_root()
    quota_category = SECTION_QUOTA_CATEGORY.get(section)
    if quota_category is None:
        raise HTTPException(status_code=400, detail=f"Unknown section '{section}'")

    # Validate the file actually exists in this section (cached tree — no
    # Drive API call) and get its real name for the Content-Disposition.
    files = list_files_recursive(_section_folder_id(section))
    meta = next((f for f in files if f["id"] == file_id), None)
    if meta is None:
        raise HTTPException(status_code=404, detail="File not found in this section")

    used = _count_today_by_category(db, current_user.id)[quota_category]
    limit = (settings.daily_paper_limit if quota_category == "paper"
             else settings.daily_book_limit)
    if used >= limit:
        raise HTTPException(status_code=429, detail="Daily download limit reached")

    db.add(DownloadLog(
        user_id=current_user.id,
        file_id=file_id,
        file_name=meta["name"],
        category=quota_category,
    ))
    db.commit()

    name = meta["name"] if meta["name"].lower().endswith(".pdf") else meta["name"] + ".pdf"
    return DownloadUrlOut(
        url=make_signed_download_url(file_id, name),
        expires_in=settings.download_link_ttl_seconds,
        quota=_quota_out(db, current_user.id),
    )
