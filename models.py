from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, Integer, String, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    # Internal-only, unique per user. Never shown to or entered by the user
    # (see routers/auth.py _generate_username) — kept so the original
    # NOT NULL/unique column doesn't require a schema change.
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=True)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    is_verified = Column(Boolean, default=False, nullable=False)
    verification_code = Column(String, nullable=True)
    code_expires_at = Column(DateTime, nullable=True)

    # lazy="noload": downloads are never accessed via this relationship in
    # request code (quota uses a COUNT query) — don't let the ORM
    # lazy-load a user's whole download history into RAM by accident.
    downloads = relationship("DownloadLog", back_populates="user", lazy="noload")


class DownloadLog(Base):
    """
    One row per successful download. Daily quota is computed by counting
    a user's rows since midnight, rather than keeping a separate counter —
    this survives server restarts (KataBump renewals) with zero extra work,
    and doubles as an audit trail / "downloads per paper" analytics later.
    """
    __tablename__ = "download_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    file_id = Column(String, nullable=False)       # Google Drive file ID
    file_name = Column(String, nullable=False)
    category = Column(String, nullable=False)      # "paper" or "book"
    downloaded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="downloads")

    # The quota query (WHERE user_id = ? AND downloaded_at >= midnight)
    # stays index-backed forever instead of degrading into a table scan.
    __table_args__ = (
        Index("ix_downloads_user_time", "user_id", "downloaded_at"),
    )
