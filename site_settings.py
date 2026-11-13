"""Database-backed admin settings shared by quota and admin endpoints."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from config import settings
from models import AdminSiteSettings

LIMIT_KEYS = {
    "papers": ("daily_paper_limit", "paper"),
    "books": ("daily_book_limit", "book"),
    "notes": ("daily_notes_limit", "notes"),
    "marking_keys": ("daily_marking_keys_limit", "marking_keys"),
}


def get_admin_settings(db: Session) -> AdminSiteSettings | None:
    return db.query(AdminSiteSettings).filter(AdminSiteSettings.id == 1).first()


def effective_limits(db: Session) -> dict[str, int]:
    row = get_admin_settings(db)
    stored = {}
    if row:
        try:
            stored = json.loads(row.limits_json or "{}")
        except (TypeError, ValueError):
            stored = {}
    return {
        key: int(stored.get(key, getattr(settings, attr)))
        for key, (attr, _) in LIMIT_KEYS.items()
    }


def category_limit(db: Session, quota_category: str) -> int:
    key = next((key for key, (_, category) in LIMIT_KEYS.items() if category == quota_category), None)
    if key is None:
        raise ValueError("Unknown quota category")
    return effective_limits(db)[key]


def public_settings(db: Session) -> dict:
    row = get_admin_settings(db)
    if row is None:
        return {"announcement": "", "maintenance": False, "featured": []}
    try:
        featured = json.loads(row.featured_json or "[]")
    except (TypeError, ValueError):
        featured = []
    return {
        "announcement": row.announcement,
        "maintenance": bool(row.maintenance),
        "featured": featured if isinstance(featured, list) else [],
    }
