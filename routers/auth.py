import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from auth import hash_password, verify_password, create_access_token, get_current_user
from config import settings
from errors import APIError
from database import get_db
from email_utils import deliver_reset_code, deliver_verification_code
from models import User, DownloadLog
from rate_limit import limit_request
from schemas import (
    UserRegister,
    UserOut,
    Token,
    QuotaOut,
    RegisterOut,
    VerifyCode,
    ResendCode,
    UpdateName,
    ForgotPassword,
    ResetPassword,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

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
    """
    All section quota counts in one indexed query.

    A row counts toward quota if it's confirmed (the bytes actually
    arrived — see POST /drive/confirm), OR if it's unconfirmed but still
    within the signed link's TTL (still possibly in flight — give it the
    benefit of the doubt). Once that window passes with no confirmation,
    the row stops counting on its own — no cleanup job needed — so a
    failed or abandoned download doesn't permanently burn the user's
    daily allowance.
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    still_pending_cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.download_link_ttl_seconds
    )
    rows = (
        db.query(DownloadLog.category, func.count(DownloadLog.id))
        .filter(
            DownloadLog.user_id == user_id,
            DownloadLog.downloaded_at >= today_start,
            or_(
                DownloadLog.confirmed.is_(True),
                DownloadLog.downloaded_at >= still_pending_cutoff,
            ),
        )
        .group_by(DownloadLog.category)
        .all()
    )
    counts = dict(rows)
    return {
        "paper": counts.get("paper", 0),
        "book": counts.get("book", 0),
        "notes": counts.get("notes", 0),
        "marking_keys": counts.get("marking_keys", 0),
    }


def _quota_out(db: Session, user_id: int) -> QuotaOut:
    used = _count_today_by_category(db, user_id)
    return QuotaOut(
        papers_downloaded_today=used["paper"],
        papers_limit=settings.daily_paper_limit,
        papers_left=max(0, settings.daily_paper_limit - used["paper"]),
        books_downloaded_today=used["book"],
        books_limit=settings.daily_book_limit,
        books_left=max(0, settings.daily_book_limit - used["book"]),
        notes_downloaded_today=used["notes"],
        notes_limit=settings.daily_notes_limit,
        notes_left=max(0, settings.daily_notes_limit - used["notes"]),
        marking_keys_downloaded_today=used["marking_keys"],
        marking_keys_limit=settings.daily_marking_keys_limit,
        marking_keys_left=max(0, settings.daily_marking_keys_limit - used["marking_keys"]),
    )


@router.post("/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED)
def register(request: Request, payload: UserRegister, db: Session = Depends(get_db)):
    email = payload.email.lower()
    limit_request(request, "auth-register", email, limit=5, window=3600, ip_limit=0)
    existing = db.query(User).filter(User.email == email).first()

    if existing and existing.is_verified:
        raise APIError(status_code=409, detail="An account with this email already exists", code="verified")
    if existing:
        raise APIError(
            status_code=409,
            detail="An account exists but its email is not verified. Enter the code or request a new one.",
            code="unverified",
        )

    code = _new_code()
    expires = datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)

    db.add(User(
        email=email,
        name=payload.name.strip(),
        username=_generate_username(db, email),
        hashed_password=hash_password(payload.password),
        is_verified=False,
        verification_code=code,
        code_expires_at=expires,
    ))
    db.commit()
    deliver_verification_code(db, email, code)
    return RegisterOut(message="Code sent to your Gmail", email=email)


MAX_VERIFY_ATTEMPTS = 5


@router.post("/verify")
def verify(request: Request, payload: VerifyCode, db: Session = Depends(get_db)):
    email = payload.email.lower()
    limit_request(request, "auth-verify", email, limit=20, window=600, ip_limit=0)
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise APIError(status_code=404, detail="Account not found", code="account_missing")
    if user.is_verified:
        return {"message": "Already verified"}
    if user.verification_attempts >= MAX_VERIFY_ATTEMPTS:
        raise APIError(
            status_code=429,
            detail="Too many incorrect attempts — request a new code",
            code="too_many_attempts",
            headers={"Retry-After": "60", "X-Attempts-Remaining": "0"},
        )
    # Constant-time compare — no timing side-channel on the code.
    if not secrets.compare_digest(user.verification_code or "", payload.code):
        user.verification_attempts += 1
        db.commit()
        raise APIError(
            status_code=400,
            detail="Incorrect code",
            code="code_invalid",
            headers={"X-Attempts-Remaining": str(max(0, MAX_VERIFY_ATTEMPTS - user.verification_attempts))},
        )
    if _expired(user.code_expires_at):
        raise APIError(
            status_code=400,
            detail="Code expired — request a new one",
            code="code_expired",
            headers={"X-Attempts-Remaining": str(max(0, MAX_VERIFY_ATTEMPTS - user.verification_attempts))},
        )

    user.is_verified = True
    user.verification_code = None
    user.code_expires_at = None
    user.verification_attempts = 0
    db.commit()
    return {"message": "Verified — you can now sign in"}


@router.post("/resend")
def resend(request: Request, payload: ResendCode, db: Session = Depends(get_db)):
    email = payload.email.lower()
    limit_request(request, "auth-resend", email, limit=5, window=3600, ip_limit=0)
    user = db.query(User).filter(User.email == email).first()
    if not user or user.is_verified:
        raise APIError(status_code=400, detail="Cannot resend a code for this account", code="resend_unavailable")

    code = _new_code()
    user.verification_code = code
    user.code_expires_at = datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)
    user.verification_attempts = 0  # fresh code = a fresh set of attempts
    db.commit()

    deliver_verification_code(db, email, code)
    return {"message": "New code sent"}


@router.post("/login", response_model=Token)
def login(
    request: Request,
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
    limit_request(request, "auth-login", identifier, limit=10, window=60, ip_limit=0)
    # Emails are always stored lowercased (register/verify/resend all
    # .lower()), so a direct equality compare keeps the unique index
    # usable — func.lower(User.email) would force a full table scan.
    user = (
        db.query(User)
        .filter((User.email == identifier) | (User.username == identifier))
        .first()
    )
    if not user or not verify_password(password, user.hashed_password):
        raise APIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            code="bad_credentials",
        )
    if not user.is_verified:
        raise APIError(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Check your Gmail for the code.",
            code="email_not_verified",
        )
    return Token(access_token=create_access_token(user))


@router.get("/me", response_model=UserOut)
def read_current_user(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/name", response_model=UserOut)
def update_name(
    payload: UpdateName,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Lets an account set/change its display name — mainly for accounts
    created before registration collected a name at all, which the
    dashboard greeting otherwise falls back to "Student" for (see
    home.js's one-time prompt).
    """
    current_user.name = payload.name.strip()
    db.commit()
    db.refresh(current_user)
    return current_user


MAX_RESET_ATTEMPTS = 5


@router.post("/forgot-password")
def forgot_password(
    request: Request,
    payload: ForgotPassword,
    db: Session = Depends(get_db),
):
    """
    Always returns the same generic message whether or not the email is
    registered — revealing that would let someone enumerate real accounts
    by trying emails here and watching which ones get a different response.
    An email only actually goes out if the account exists and is verified.
    """
    email = payload.email.lower()
    limit_request(request, "auth-forgot", email, limit=5, window=3600, ip_limit=0)
    user = db.query(User).filter(User.email == email).first()
    if user and user.is_verified:
        code = _new_code()
        user.reset_code = code
        user.reset_code_expires_at = datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)
        user.reset_attempts = 0
        db.commit()
        try:
            deliver_reset_code(db, email, code)
        except APIError as exc:
            # Preserve the endpoint's account-enumeration-safe response.
            logger.warning("Password reset email could not be sent (%s)", exc.code)
    return {"message": "If that email has an account, a reset code has been sent"}


@router.post("/reset-password")
def reset_password(request: Request, payload: ResetPassword, db: Session = Depends(get_db)):
    email = payload.email.lower()
    limit_request(request, "auth-reset", email, limit=20, window=600, ip_limit=0)
    user = db.query(User).filter(User.email == email).first()
    # Same "Incorrect or expired code" message whether the account doesn't
    # exist, no reset was ever requested, the code's wrong, or it expired —
    # doesn't leak which case it is, same principle as the endpoint above.
    generic_error = APIError(status_code=400, detail="Incorrect or expired code", code="code_invalid")

    if not user or not user.reset_code:
        raise generic_error
    if user.reset_attempts >= MAX_RESET_ATTEMPTS:
        raise APIError(
            status_code=429,
            detail="Too many incorrect attempts — request a new code",
            code="too_many_attempts",
            headers={"Retry-After": "60", "X-Attempts-Remaining": "0"},
        )
    if not secrets.compare_digest(user.reset_code, payload.code):
        user.reset_attempts += 1
        db.commit()
        raise APIError(
            status_code=400,
            detail="Incorrect or expired code",
            code="code_invalid",
            headers={"X-Attempts-Remaining": str(max(0, MAX_RESET_ATTEMPTS - user.reset_attempts))},
        )
    if _expired(user.reset_code_expires_at):
        raise APIError(
            status_code=400,
            detail="Incorrect or expired code",
            code="code_invalid",
            headers={"X-Attempts-Remaining": str(max(0, MAX_RESET_ATTEMPTS - user.reset_attempts))},
        )

    user.hashed_password = hash_password(payload.new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    user.reset_code = None
    user.reset_code_expires_at = None
    user.reset_attempts = 0
    db.commit()
    return {"message": "Password updated — you can now sign in"}


@router.get("/quota", response_model=QuotaOut)
def get_quota(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Computed from today's DownloadLog rows rather than a stored counter —
    survives server restarts with no extra bookkeeping, and resets itself
    naturally at midnight since "today" always means the current date.
    """
    return _quota_out(db, current_user.id)
