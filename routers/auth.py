import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import hash_password, verify_password, create_access_token, get_current_user
from config import settings
from database import get_db
from email_utils import send_code
from models import User, DownloadLog
from schemas import (
    UserRegister,
    UserOut,
    Token,
    QuotaOut,
    RegisterOut,
    VerifyCode,
    ResendCode,
)

router = APIRouter(prefix="/auth", tags=["auth"])

CODE_TTL_MINUTES = 10


def _generate_username(db: Session, email: str) -> str:
    """
    Derives an internal, unique username from the email's local part.
    Purely internal bookkeeping — the user never sees or enters this —
    so the pre-existing NOT NULL/unique username column keeps working
    without needing a schema change.
    """
    base = email.split("@")[0].strip().lower() or "user"
    # One query instead of one-per-candidate: grab every taken variant.
    taken = {
        row[0]
        for row in db.query(User.username).filter(User.username.like(f"{base}%")).all()
    }
    if base not in taken:
        return base
    suffix = 2
    while f"{base}{suffix}" in taken:
        suffix += 1
    return f"{base}{suffix}"


def _new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def _expired(expires_at: datetime | None) -> bool:
    """
    SQLite hands back NAIVE datetimes; datetime.now(timezone.utc) is AWARE.
    Comparing them directly raises TypeError (a 500 on every expired code
    check) — normalize before comparing.
    """
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


def _count_today_by_category(db: Session, user_id: int) -> dict[str, int]:
    """Both quota counts in ONE indexed query instead of two."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        db.query(DownloadLog.category, func.count(DownloadLog.id))
        .filter(DownloadLog.user_id == user_id, DownloadLog.downloaded_at >= today_start)
        .group_by(DownloadLog.category)
        .all()
    )
    counts = dict(rows)
    return {"paper": counts.get("paper", 0), "book": counts.get("book", 0)}


def _quota_out(db: Session, user_id: int) -> QuotaOut:
    used = _count_today_by_category(db, user_id)
    return QuotaOut(
        papers_downloaded_today=used["paper"],
        papers_limit=settings.daily_paper_limit,
        papers_left=max(0, settings.daily_paper_limit - used["paper"]),
        books_downloaded_today=used["book"],
        books_limit=settings.daily_book_limit,
        books_left=max(0, settings.daily_book_limit - used["book"]),
    )


@router.post("/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserRegister, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    email = payload.email.lower()
    existing = db.query(User).filter(User.email == email).first()

    if existing and existing.is_verified:
        raise HTTPException(status_code=400, detail="An account with this email already exists")

    code = _new_code()
    expires = datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)

    if existing:
        # Unverified leftover from an earlier attempt — refresh it instead of
        # bricking the email address. New password wins (they're re-registering).
        existing.hashed_password = hash_password(payload.password)
        existing.verification_code = code
        existing.code_expires_at = expires
    else:
        db.add(User(
            email=email,
            username=_generate_username(db, email),
            hashed_password=hash_password(payload.password),
            is_verified=False,
            verification_code=code,
            code_expires_at=expires,
        ))
    db.commit()

    # Returns instantly; SMTP happens after the response. Failures are logged
    # by email_utils and the user can hit /auth/resend.
    background_tasks.add_task(send_code, email, code)

    return RegisterOut(message="Code sent to your Gmail", email=email)


@router.post("/verify")
def verify(payload: VerifyCode, db: Session = Depends(get_db)):
    email = payload.email.lower()
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Account not found")
    if user.is_verified:
        return {"message": "Already verified"}
    # Constant-time compare — no timing side-channel on the code.
    if not secrets.compare_digest(user.verification_code or "", payload.code):
        raise HTTPException(status_code=400, detail="Incorrect code")
    if _expired(user.code_expires_at):
        raise HTTPException(status_code=400, detail="Code expired — request a new one")

    user.is_verified = True
    user.verification_code = None
    user.code_expires_at = None
    db.commit()
    return {"message": "Verified — you can now sign in"}


@router.post("/resend")
def resend(payload: ResendCode, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    email = payload.email.lower()
    user = db.query(User).filter(User.email == email).first()
    if not user or user.is_verified:
        raise HTTPException(status_code=400, detail="Cannot resend a code for this account")

    code = _new_code()
    user.verification_code = code
    user.code_expires_at = datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)
    db.commit()

    background_tasks.add_task(send_code, email, code)
    return {"message": "New code sent"}


@router.post("/login", response_model=Token)
def login(
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Looks up by email, falling back to the old `username` column so
    accounts created before the email migration can still log in with
    their old username in this same field.
    """
    identifier = email.strip().lower()
    # Emails are always stored lowercased (register/verify/resend all
    # .lower()), so a direct equality compare keeps the unique index
    # usable — func.lower(User.email) would force a full table scan.
    user = (
        db.query(User)
        .filter((User.email == identifier) | (User.username == identifier))
        .first()
    )
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Check your Gmail for the code.",
        )
    return Token(access_token=create_access_token(user.username))


@router.get("/me", response_model=UserOut)
def read_current_user(current_user: User = Depends(get_current_user)):
    return current_user


@router.get("/quota", response_model=QuotaOut)
def get_quota(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Computed from today's DownloadLog rows rather than a stored counter —
    survives server restarts with no extra bookkeeping, and resets itself
    naturally at midnight since "today" always means the current date.
    """
    return _quota_out(db, current_user.id)
