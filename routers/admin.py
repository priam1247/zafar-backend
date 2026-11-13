"""Admin API: database-backed views and audited mutations only."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field, StrictInt, field_validator
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

import database
from auth import get_current_user
from config import settings
from email_utils import deliver_reset_code
from errors import APIError
from models import (
    AdminAuditLog,
    AdminReport,
    AdminRequest,
    AdminSiteSettings,
    ApiErrorEvent,
    DownloadLog,
    SearchLog,
    User,
)
from rate_limit import limit_request
from routers.auth import _new_code
from routers.drive import SECTION_FOLDERS, _list_section_files
from site_settings import LIMIT_KEYS, effective_limits, get_admin_settings

router = APIRouter(prefix="/admin", tags=["admin"])
_ADMIN_ROLES = {"owner", "editor", "viewer"}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def admin_access(*roles: str):
    allowed = set(roles)

    def dependency(request: Request, user: User = Depends(get_current_user)) -> User:
        limit_request(request, "admin", user.email or str(user.id), limit=60, window=60, ip_limit=0)
        if user.role not in _ADMIN_ROLES or (allowed and user.role not in allowed):
            raise APIError(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This admin action is not allowed for this account",
                code="admin_forbidden",
            )
        return user

    return dependency


def _d1_audited_batch(
    db: Session,
    actor: User,
    action: str,
    target: str,
    detail: dict | None,
    statements: list[tuple[str, tuple]],
) -> int:
    """Run mutation SQL and its audit insert in one Cloudflare D1 batch."""
    raw = database.engine.raw_connection()
    try:
        connection = raw.driver_connection
        audit_sql = (
            "INSERT INTO admin_audit_log "
            "(at, actor_user_id, actor_email, action, target, detail) "
            "SELECT ?, ?, ?, ?, ?, ? WHERE changes() > 0"
        )
        audit_params = (
            datetime.now(timezone.utc), actor.id, actor.email or "", action, target,
            json.dumps(detail or {}, separators=(",", ":"), sort_keys=True),
        )
        results = connection.execute_batch([*statements, (audit_sql, audit_params)])
        changed = int((results[-2].get("meta") or {}).get("changes") or 0)
    finally:
        raw.close()
    db.expire_all()
    return changed


def _audit(db: Session, actor: User, action: str, target: str, detail: dict | None = None) -> None:
    db.add(AdminAuditLog(
        actor_user_id=actor.id,
        actor_email=actor.email or "",
        action=action,
        target=target,
        detail=json.dumps(detail or {}, separators=(",", ":"), sort_keys=True),
    ))


def _user_out(db: Session, user: User, downloads: int | None = None) -> dict:
    if downloads is None:
        downloads = db.query(func.count(DownloadLog.id)).filter(
            DownloadLog.user_id == user.id, DownloadLog.confirmed.is_(True)
        ).scalar() or 0
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "suspended": bool(user.suspended),
        "is_verified": bool(user.is_verified),
        "signup_method": user.signup_method,
        "created_at": _iso(user.created_at),
        "last_active_at": _iso(user.last_active_at),
        "downloads": int(downloads),
    }


def _keep_last_owner(db: Session, target: User, *, deleting: bool = False) -> None:
    if target.role == "owner":
        active_owners = db.query(User.id).filter(
            User.role == "owner", User.suspended.is_(False)
        ).with_for_update().all()
        count = len(active_owners)
        if count <= 1 and (deleting or target.suspended is False):
            raise APIError(409, "The last active owner cannot be removed or suspended", "last_owner")


class SuspendBody(BaseModel):
    suspended: bool
    reason: str | None = Field(default=None, max_length=500)


class RoleBody(BaseModel):
    role: Literal["owner", "editor", "viewer"] | None


class StatusBody(BaseModel):
    status: str


class SettingsBody(BaseModel):
    announcement: str | None = Field(default=None, max_length=2000)
    maintenance: bool | None = None
    featured: list[str] | None = Field(default=None, max_length=100)
    limits: dict[str, StrictInt] | None = None

    @field_validator("featured")
    @classmethod
    def clean_featured(cls, value):
        if value is not None and any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in value):
            raise ValueError("featured entries must be non-empty file IDs under 257 characters")
        return value

    @field_validator("limits")
    @classmethod
    def clean_limits(cls, value):
        if value is not None:
            unknown = set(value) - set(LIMIT_KEYS)
            if unknown or any(isinstance(v, bool) or v < 0 or v > 1000 for v in value.values()):
                raise ValueError("limits must contain supported keys and values from 0 to 1000")
        return value


@router.get("/whoami")
def whoami(actor: User = Depends(admin_access("owner", "editor", "viewer"))):
    return {"email": actor.email, "name": actor.name, "role": actor.role}


@router.get("/overview")
def overview(
    days: int = Query(14, ge=1, le=90),
    actor: User = Depends(admin_access("owner", "editor", "viewer")),
    db: Session = Depends(database.get_db),
):
    del actor
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    new_users = db.query(User.created_at).filter(User.created_at >= start, User.created_at < end).all()
    confirmed = db.query(DownloadLog).filter(
        DownloadLog.confirmed.is_(True), DownloadLog.downloaded_at >= start, DownloadLog.downloaded_at < end,
    ).all()
    cutoff = now - timedelta(seconds=settings.download_link_ttl_seconds)
    failed = db.query(DownloadLog).filter(
        DownloadLog.confirmed.is_(False), DownloadLog.downloaded_at >= start,
        DownloadLog.downloaded_at < end, DownloadLog.downloaded_at < cutoff,
    ).all()
    daily = {}
    for offset in range(days):
        day = (start + timedelta(days=offset)).date().isoformat()
        daily[day] = {"date": day, "signups": 0, "downloads": 0, "failed": 0}
    for (created_at,) in new_users:
        day = created_at.date().isoformat() if created_at else None
        if day in daily:
            daily[day]["signups"] += 1
    for row in confirmed:
        at = row.downloaded_at
        day = at.date().isoformat() if at else None
        if day in daily:
            daily[day]["downloads"] += 1
    for row in failed:
        at = row.downloaded_at
        day = at.date().isoformat() if at else None
        if day in daily:
            daily[day]["failed"] += 1

    total_users = db.query(func.count(User.id)).scalar() or 0
    verified_users = db.query(func.count(User.id)).filter(User.is_verified.is_(True)).scalar() or 0
    new_today = db.query(func.count(User.id)).filter(User.created_at >= today, User.created_at < end).scalar() or 0
    total_downloads = db.query(func.count(DownloadLog.id)).filter(DownloadLog.confirmed.is_(True)).scalar() or 0
    today_key = today.date().isoformat()
    today_downloads = daily[today_key]["downloads"]
    today_failed = daily[today_key]["failed"]

    file_counts: dict[tuple[str, str, str], int] = {}
    for row in confirmed:
        section = {"paper": "papers", "book": "books", "marking_keys": "marking-keys"}.get(row.category, row.category)
        key = (row.file_id, row.file_name, section)
        file_counts[key] = file_counts.get(key, 0) + 1
        # Only show top subjects when a trustworthy subject field is added;
        # filenames/folder labels are not parsed into invented categories.
    top_files = [
        {"id": file_id, "name": name, "section": section, "downloads": count}
        for (file_id, name, section), count in sorted(file_counts.items(), key=lambda kv: (-kv[1], kv[0][1].casefold()))[:10]
    ]
    zero_rows = db.query(SearchLog.q, func.count(SearchLog.id).label("count")).filter(
        SearchLog.results == 0, SearchLog.at >= start, SearchLog.at < end,
    ).group_by(SearchLog.q).order_by(func.count(SearchLog.id).desc()).limit(10).all()
    return {
        "users": {"total": int(total_users), "verified": int(verified_users), "new_today": int(new_today)},
        "downloads": {"today": today_downloads, "total": int(total_downloads), "failed_today": today_failed},
        "daily": list(daily.values()),
        "top_files": top_files,
        # No subject is stored in the production schema; omit rather than guess.
        "zero_result_searches": [{"q": q, "count": int(count)} for q, count in zero_rows],
    }


@router.get("/users")
def list_users(
    q: str = Query("", max_length=200),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: User = Depends(admin_access("owner")),
    db: Session = Depends(database.get_db),
):
    del actor
    query = db.query(User)
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(or_(User.email.ilike(needle), User.name.ilike(needle)))
    total = query.count()
    users = query.order_by(User.created_at.desc(), User.id.desc()).offset(offset).limit(limit).all()
    ids = [user.id for user in users]
    counts = {}
    if ids:
        counts = dict(
            db.query(DownloadLog.user_id, func.count(DownloadLog.id))
            .filter(DownloadLog.user_id.in_(ids), DownloadLog.confirmed.is_(True))
            .group_by(DownloadLog.user_id).all()
        )
    result = [_user_out(db, user, int(counts.get(user.id, 0))) for user in users]
    return Response(
        content=json.dumps(result),
        media_type="application/json",
        headers={"X-Total-Count": str(total), "Cache-Control": "no-store"},
    )


@router.post("/users/{user_id}/suspend")
def suspend_user(
    user_id: int,
    payload: SuspendBody,
    actor: User = Depends(admin_access("owner")),
    db: Session = Depends(database.get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise APIError(404, "Account not found", "admin_user_missing")
    if target.id == actor.id and payload.suspended:
        raise APIError(409, "You cannot suspend your own active session", "self_suspend")
    action = "user.suspend" if payload.suspended else "user.restore"
    detail = {"reason": payload.reason}
    if database.IS_D1:
        if payload.suspended:
            _keep_last_owner(db, target)
        changed = _d1_audited_batch(
            db, actor, action, str(target.id), detail,
            [("UPDATE users SET suspended = ? WHERE id = ? AND "
              "(role IS NULL OR role <> 'owner' OR ? = 0 OR "
              "(SELECT COUNT(*) FROM users WHERE role = 'owner' AND suspended = 0) > 1)",
              (payload.suspended, target.id, payload.suspended))],
        )
        if not changed:
            raise APIError(409, "The last active owner cannot be suspended", "last_owner")
        target = db.query(User).filter(User.id == user_id).first()
        return _user_out(db, target)
    if payload.suspended:
        _keep_last_owner(db, target)
    target.suspended = payload.suspended
    _audit(db, actor, action, str(target.id), detail)
    db.commit()
    return _user_out(db, target)


@router.post("/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    actor: User = Depends(admin_access("owner")),
    db: Session = Depends(database.get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if target is None or not target.email or not target.is_verified:
        raise APIError(404, "Verified account not found", "admin_user_missing")
    code = _new_code()
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    if database.IS_D1:
        changed = _d1_audited_batch(
            db, actor, "user.reset_password", str(target.id), {},
            [("UPDATE users SET reset_code = ?, reset_code_expires_at = ?, reset_attempts = 0 WHERE id = ?",
              (code, expires, target.id))],
        )
        if not changed:
            raise APIError(404, "Verified account not found", "admin_user_missing")
        db.expire_all()
    else:
        target.reset_code = code
        target.reset_code_expires_at = expires
        target.reset_attempts = 0
        _audit(db, actor, "user.reset_password", str(target.id), {})
        db.commit()
    sent = deliver_reset_code(db, target.email, code)
    return {"sent": sent}


@router.patch("/users/{user_id}")
def change_role(
    user_id: int,
    payload: RoleBody,
    actor: User = Depends(admin_access("owner")),
    db: Session = Depends(database.get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise APIError(404, "Account not found", "admin_user_missing")
    if payload.role == "owner" and target.suspended:
        raise APIError(409, "Restore the account before assigning the owner role", "owner_account_suspended")
    if payload.role == "owner" and (not target.admin_totp_enabled or not target.admin_totp_secret):
        raise APIError(409, "Enroll the target account's authenticator with the owner bootstrap CLI before assigning owner", "owner_mfa_not_enrolled")
    if target.role == payload.role:
        return _user_out(db, target)
    if target.role == "owner" and payload.role != "owner":
        _keep_last_owner(db, target, deleting=True)
        if target.id == actor.id:
            raise APIError(409, "You cannot remove your own owner role", "self_role_change")
    old_role = target.role
    if database.IS_D1:
        changed = _d1_audited_batch(
            db, actor, "user.role", str(target.id), {"from": old_role, "to": payload.role},
            [("UPDATE users SET role = ? WHERE id = ? AND "
              "(role IS NULL OR role <> 'owner' OR ? = 'owner' OR "
              "(SELECT COUNT(*) FROM users WHERE role = 'owner' AND suspended = 0) > 1)",
              (payload.role, target.id, payload.role))],
        )
        if not changed:
            raise APIError(409, "The last active owner cannot lose the owner role", "last_owner")
        target = db.query(User).filter(User.id == user_id).first()
        return _user_out(db, target)
    target.role = payload.role
    _audit(db, actor, "user.role", str(target.id), {"from": old_role, "to": payload.role})
    db.commit()
    return _user_out(db, target)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    actor: User = Depends(admin_access("owner")),
    db: Session = Depends(database.get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise APIError(404, "Account not found", "admin_user_missing")
    if target.id == actor.id:
        raise APIError(409, "You cannot delete your own active session", "self_delete")
    _keep_last_owner(db, target, deleting=True)
    target_email = target.email or ""
    if database.IS_D1:
        changed = _d1_audited_batch(
            db, actor, "user.delete", str(target.id), {"email": target_email},
            [
                ("DELETE FROM download_logs WHERE user_id = ?", (target.id,)),
                ("DELETE FROM users WHERE id = ? AND "
                 "(role IS NULL OR role <> 'owner' OR "
                 "(SELECT COUNT(*) FROM users WHERE role = 'owner' AND suspended = 0) > 1)",
                 (target.id,)),
            ],
        )
        if not changed:
            raise APIError(409, "The last active owner cannot be deleted", "last_owner")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    db.query(DownloadLog).filter(DownloadLog.user_id == target.id).delete(synchronize_session=False)
    _audit(db, actor, "user.delete", str(target.id), {"email": target_email})
    db.delete(target)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _request_item(row: AdminRequest) -> dict:
    return {"id": row.id, "email": row.email, "section": row.section, "title": row.title,
            "note": row.note, "status": row.status, "created_at": _iso(row.created_at)}


def _report_item(row: AdminReport) -> dict:
    return {"id": row.id, "email": row.email, "file_id": row.file_id, "file_name": row.file_name,
            "reason": row.reason, "note": row.note, "status": row.status, "created_at": _iso(row.created_at)}


def _validate_featured_ids(ids: list[str]) -> None:
    if not ids:
        return
    available = set()
    try:
        for section in SECTION_FOLDERS:
            available.update(item.get("id") for item in _list_section_files(section) if item.get("id"))
    except Exception as exc:
        raise APIError(503, "The library could not validate featured files", "admin_featured_validation_unavailable") from exc
    unknown = sorted(set(ids) - available)
    if unknown:
        raise APIError(422, "Featured items must be existing library file IDs", "admin_featured_file_missing")


@router.get("/requests")
def list_requests(
    status_filter: str = Query("open", alias="status", pattern="^(open|done|all)$"),
    limit: int = Query(50, ge=1, le=100),
    actor: User = Depends(admin_access("owner", "editor")),
    db: Session = Depends(database.get_db),
):
    del actor
    query = db.query(AdminRequest)
    if status_filter != "all":
        query = query.filter(AdminRequest.status == status_filter)
    return [_request_item(row) for row in query.order_by(AdminRequest.created_at.desc()).limit(limit).all()]


@router.patch("/requests/{request_id}")
def update_request(
    request_id: int,
    payload: StatusBody,
    actor: User = Depends(admin_access("owner", "editor")),
    db: Session = Depends(database.get_db),
):
    if payload.status != "done":
        raise APIError(422, "Request status must be done", "admin_status_invalid")
    row = db.query(AdminRequest).filter(AdminRequest.id == request_id).first()
    if row is None:
        raise APIError(404, "Request not found", "admin_request_missing")
    resolved_at = datetime.now(timezone.utc)
    if database.IS_D1:
        changed = _d1_audited_batch(
            db, actor, "request.done", str(row.id), {},
            [("UPDATE admin_requests SET status = 'done', resolved_at = ? WHERE id = ?", (resolved_at, row.id))],
        )
        if not changed:
            raise APIError(404, "Request not found", "admin_request_missing")
        row = db.query(AdminRequest).filter(AdminRequest.id == request_id).first()
        return _request_item(row)
    row.status = "done"
    row.resolved_at = resolved_at
    _audit(db, actor, "request.done", str(row.id), {})
    db.commit()
    return _request_item(row)


@router.get("/reports")
def list_reports(
    status_filter: str = Query("open", alias="status", pattern="^(open|resolved|all)$"),
    limit: int = Query(50, ge=1, le=100),
    actor: User = Depends(admin_access("owner", "editor")),
    db: Session = Depends(database.get_db),
):
    del actor
    query = db.query(AdminReport)
    if status_filter != "all":
        query = query.filter(AdminReport.status == status_filter)
    return [_report_item(row) for row in query.order_by(AdminReport.created_at.desc()).limit(limit).all()]


@router.patch("/reports/{report_id}")
def update_report(
    report_id: int,
    payload: StatusBody,
    actor: User = Depends(admin_access("owner", "editor")),
    db: Session = Depends(database.get_db),
):
    if payload.status != "resolved":
        raise APIError(422, "Report status must be resolved", "admin_status_invalid")
    row = db.query(AdminReport).filter(AdminReport.id == report_id).first()
    if row is None:
        raise APIError(404, "Report not found", "admin_report_missing")
    resolved_at = datetime.now(timezone.utc)
    if database.IS_D1:
        changed = _d1_audited_batch(
            db, actor, "report.resolved", str(row.id), {},
            [("UPDATE admin_reports SET status = 'resolved', resolved_at = ? WHERE id = ?", (resolved_at, row.id))],
        )
        if not changed:
            raise APIError(404, "Report not found", "admin_report_missing")
        row = db.query(AdminReport).filter(AdminReport.id == report_id).first()
        return _report_item(row)
    row.status = "resolved"
    row.resolved_at = resolved_at
    _audit(db, actor, "report.resolved", str(row.id), {})
    db.commit()
    return _report_item(row)


@router.get("/settings")
def read_settings(
    actor: User = Depends(admin_access("owner")),
    db: Session = Depends(database.get_db),
):
    del actor
    row = get_admin_settings(db)
    if row is None:
        announcement, maintenance, featured = "", False, []
    else:
        announcement, maintenance = row.announcement, bool(row.maintenance)
        try:
            featured = json.loads(row.featured_json or "[]")
        except ValueError:
            featured = []
    return {"announcement": announcement, "maintenance": maintenance,
            "featured": featured, "limits": effective_limits(db)}


@router.patch("/settings")
def update_settings(
    payload: SettingsBody,
    actor: User = Depends(admin_access("owner")),
    db: Session = Depends(database.get_db),
):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return read_settings(actor, db)
    if "featured" in changes:
        _validate_featured_ids(changes["featured"] or [])
    row = get_admin_settings(db)
    if database.IS_D1:
        current_public = read_settings(actor, db)
        announcement = changes.get("announcement", current_public["announcement"])
        maintenance = changes.get("maintenance", current_public["maintenance"])
        featured = changes.get("featured", current_public["featured"])
        limits = dict(current_public["limits"])
        limits.update(changes.get("limits") or {})
        now = datetime.now(timezone.utc)
        statement = (
            "INSERT INTO admin_site_settings "
            "(id, announcement, maintenance, featured_json, limits_json, updated_at, updated_by) "
            "VALUES (1, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "announcement=excluded.announcement, maintenance=excluded.maintenance, "
            "featured_json=excluded.featured_json, limits_json=excluded.limits_json, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by"
        )
        changed = _d1_audited_batch(
            db, actor, "settings.update", "site", {"fields": sorted(changes)},
            [(statement, (announcement, maintenance, json.dumps(featured, separators=(",", ":")),
                          json.dumps(limits, separators=(",", ":"), sort_keys=True), now, actor.id))],
        )
        if not changed:
            raise APIError(503, "Could not persist site settings", "admin_settings_unavailable")
        return read_settings(actor, db)
    if row is None:
        row = AdminSiteSettings(id=1)
        db.add(row)
    if "announcement" in changes:
        row.announcement = changes["announcement"] or ""
    if "maintenance" in changes:
        row.maintenance = bool(changes["maintenance"])
    if "featured" in changes:
        row.featured_json = json.dumps(changes["featured"] or [], separators=(",", ":"))
    if "limits" in changes:
        limits = effective_limits(db)
        limits.update(changes["limits"] or {})
        row.limits_json = json.dumps(limits, separators=(",", ":"), sort_keys=True)
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = actor.id
    _audit(db, actor, "settings.update", "site", {"fields": sorted(changes)})
    db.commit()
    # Return the state the running quota code now reads from persistent DB.
    return read_settings(actor, db)


@router.get("/audit")
def list_audit(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    actor: User = Depends(admin_access("owner")),
    db: Session = Depends(database.get_db),
):
    del actor
    rows = db.query(AdminAuditLog).order_by(AdminAuditLog.at.desc(), AdminAuditLog.id.desc()).offset(offset).limit(limit).all()
    return [{"id": row.id, "at": _iso(row.at), "actor_email": row.actor_email,
             "action": row.action, "target": row.target, "detail": row.detail} for row in rows]


@router.get("/ops")
def operations(
    actor: User = Depends(admin_access("owner")),
    db: Session = Depends(database.get_db),
):
    del actor
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    recent = db.query(ApiErrorEvent.route, ApiErrorEvent.status, func.count(ApiErrorEvent.id).label("count"),
                      func.max(ApiErrorEvent.at).label("at")).filter(
        ApiErrorEvent.at >= now - timedelta(days=7),
    ).group_by(ApiErrorEvent.route, ApiErrorEvent.status).order_by(func.max(ApiErrorEvent.at).desc()).limit(50).all()
    errors_today = db.query(func.count(ApiErrorEvent.id)).filter(ApiErrorEvent.at >= day_start).scalar() or 0
    result = {
        "db": {"kind": "cloudflare-d1" if database.IS_D1 else database.engine.dialect.name},
        "errors": [{"at": _iso(row.at), "route": row.route, "status": row.status, "count": int(row.count)} for row in recent],
        "errors_today": int(errors_today),
    }
    # SQLite file size is directly measurable. For remote DBs we omit size
    # rather than report a guessed value or require a privileged server query.
    if database.engine.dialect.name == "sqlite" and not database.IS_D1:
        db_path = database.engine.url.database
        if db_path and db_path != ":memory:" and os.path.isfile(db_path):
            result["db"]["size_bytes"] = os.path.getsize(db_path)
    return result


def _csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _export_sig(kind: str, exp: int, nonce: str) -> str:
    message = f"admin-export-v1:{kind}:{exp}:{nonce}".encode()
    return hmac.new(settings.download_signing_secret.encode(), message, hashlib.sha256).hexdigest()


@router.post("/export/{kind}")
def create_export(
    kind: Literal["users", "files"],
    request: Request,
    actor: User = Depends(admin_access("owner")),
    db: Session = Depends(database.get_db),
):
    _audit(db, actor, "export.create", kind, {})
    db.commit()
    exp = int(time.time()) + 300
    nonce = os.urandom(16).hex()
    sig = _export_sig(kind, exp, nonce)
    url = f"{str(request.base_url).rstrip('/')}/admin/export-download?kind={kind}&exp={exp}&nonce={nonce}&sig={sig}"
    return {"url": url, "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat()}


@router.get("/export-download", name="admin_export_download")
def download_export(
    kind: Literal["users", "files"],
    exp: int,
    nonce: str,
    sig: str,
    db: Session = Depends(database.get_db),
):
    now = int(time.time())
    if exp < now or exp > now + 300 or len(nonce) != 32 or not hmac.compare_digest(_export_sig(kind, exp, nonce), sig):
        raise APIError(403, "Export link is invalid or expired", "admin_export_invalid")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    if kind == "users":
        writer.writerow(["id", "email", "name", "role", "suspended", "verified", "signup_method", "created_at", "last_active_at", "downloads"])
        counts = dict(db.query(DownloadLog.user_id, func.count(DownloadLog.id))
                      .filter(DownloadLog.confirmed.is_(True)).group_by(DownloadLog.user_id).all())
        users = db.query(User).order_by(User.id.asc()).yield_per(500)
        for user in users:
            writer.writerow([user.id, _csv_cell(user.email or ""), _csv_cell(user.name or ""),
                             user.role or "student", bool(user.suspended), bool(user.is_verified),
                             user.signup_method or "unknown", _iso(user.created_at) or "",
                             _iso(user.last_active_at) or "", int(counts.get(user.id, 0))])
    else:
        writer.writerow(["id", "name", "section", "category", "size_bytes"])
        files = {}
        for section in SECTION_FOLDERS:
            try:
                for item in _list_section_files(section):
                    if item.get("mimeType") == "application/pdf":
                        files[item["id"]] = [_csv_cell(item["id"]), _csv_cell(item.get("name", "")),
                                             section, _csv_cell(item.get("category", "")), item.get("size", "")]
            except Exception:
                # The export never fabricates a partial row or hides source failure.
                raise APIError(502, "The library could not be reached for export", "admin_export_source_failed")
        writer.writerows(files.values())
    body = output.getvalue()
    return Response(content=body, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="studymw-{kind}.csv"',
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex, nofollow",
    })
