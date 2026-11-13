"""Student submissions for the admin moderation queues."""
from __future__ import annotations

from datetime import timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

import database
from auth import get_current_user
from errors import APIError
from models import AdminReport, AdminRequest, User
from rate_limit import limit_request
from routers.drive import SECTION_FOLDERS, _list_section_files

router = APIRouter(tags=["student submissions"])
Section = Literal["papers", "primary", "books", "notes", "marking-keys"]


class RequestSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: Section
    title: str = Field(min_length=1, max_length=160)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("note")
    @classmethod
    def clean_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ReportSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=160)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("file_id", "reason")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("note")
    @classmethod
    def clean_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


def _timestamp(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _actor_email(user: User) -> str:
    # Normal accounts are created with an email, but keep the queue's attribution
    # explicit even if a legacy account has no email value.
    return user.email or f"user:{user.id}"


@router.post("/requests", status_code=status.HTTP_201_CREATED)
def submit_request(
    payload: RequestSubmission,
    request: Request,
    actor: User = Depends(get_current_user),
    db: Session = Depends(database.get_db),
):
    """Let a signed-in student request library material for a section."""
    limit_request(request, "student-request", str(actor.id), limit=3, window=600, ip_limit=0)
    row = AdminRequest(
        email=_actor_email(actor),
        section=payload.section,
        title=payload.title,
        note=payload.note,
        status="open",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "section": row.section,
        "title": row.title,
        "note": row.note,
        "status": row.status,
        "created_at": _timestamp(row.created_at),
    }


def _find_live_file(file_id: str) -> dict | None:
    try:
        for section in SECTION_FOLDERS:
            for item in _list_section_files(section):
                if str(item.get("id", "")) == file_id:
                    return item
    except Exception as exc:
        raise APIError(
            503,
            "The library could not validate this file report",
            "report_validation_unavailable",
        ) from exc
    return None


@router.post("/reports", status_code=status.HTTP_201_CREATED)
def submit_report(
    payload: ReportSubmission,
    request: Request,
    actor: User = Depends(get_current_user),
    db: Session = Depends(database.get_db),
):
    """Report a real library file; file names come from the library, not input."""
    limit_request(request, "student-report", str(actor.id), limit=5, window=600, ip_limit=0)
    item = _find_live_file(payload.file_id)
    if item is None:
        raise APIError(404, "The reported file is not in the current library", "report_file_missing")
    row = AdminReport(
        email=_actor_email(actor),
        file_id=payload.file_id,
        file_name=str(item.get("name") or ""),
        reason=payload.reason,
        note=payload.note,
        status="open",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "file_id": row.file_id,
        "file_name": row.file_name,
        "reason": row.reason,
        "note": row.note,
        "status": row.status,
        "created_at": _timestamp(row.created_at),
    }
