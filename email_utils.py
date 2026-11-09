import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings
from errors import APIError

logger = logging.getLogger(__name__)


class EmailDeliveryError(RuntimeError):
    """An SMTP attempt failed; the API must not report successful delivery."""


def reserve_daily_email_send(db: Session) -> int:
    """Atomically reserve one of the durable per-UTC-day Gmail send slots."""
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    result = db.execute(
        text(
            """
            INSERT INTO email_send_counters (day, count) VALUES (:day, 1)
            ON CONFLICT(day) DO UPDATE
                SET count = email_send_counters.count + 1
                WHERE email_send_counters.count < :daily_limit
            """
        ),
        {"day": day, "daily_limit": settings.email_daily_limit},
    )
    reserved = result.rowcount > 0
    db.commit()
    if not reserved:
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        retry_after = max(1, int((tomorrow - now).total_seconds()))
        raise APIError(
            status_code=429,
            detail="Daily email limit reached. Please try again tomorrow.",
            code="email_daily_limit",
            headers={"Retry-After": str(retry_after)},
        )
    count = db.execute(
        text("SELECT count FROM email_send_counters WHERE day = :day"), {"day": day}
    ).scalar_one()
    if count >= settings.email_daily_warn_threshold:
        logger.warning("Gmail daily send counter reached %s/%s", count, settings.email_daily_limit)
    return int(count)


def _send(email: str, subject: str, body: str) -> None:
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = email
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_pass)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailDeliveryError("SMTP delivery failed") from exc


def send_code(email: str, code: str) -> None:
    _send(
        email,
        "Your Zafar verification code",
        f"Your Zafar verification code is: {code}\n\n"
        "This code expires in 10 minutes. If you didn't request this, you can ignore this email.",
    )


def send_reset_code(email: str, code: str) -> None:
    _send(
        email,
        "Your Zafar password reset code",
        f"Your Zafar password reset code is: {code}\n\n"
        "This code expires in 10 minutes. If you didn't request this, "
        "you can safely ignore this email — your password will stay unchanged.",
    )


def deliver_verification_code(db: Session, email: str, code: str) -> None:
    reserve_daily_email_send(db)
    try:
        send_code(email, code)
    except EmailDeliveryError as exc:
        logger.exception("Verification email delivery failed")
        raise APIError(
            status_code=503,
            detail="We could not send your verification email. Please try again shortly.",
            code="email_delivery_failed",
        ) from exc


def deliver_reset_code(db: Session, email: str, code: str) -> None:
    reserve_daily_email_send(db)
    try:
        send_reset_code(email, code)
    except EmailDeliveryError:
        # Reset is deliberately enumeration-safe, so the caller still gets
        # the same generic response for known and unknown addresses.
        logger.exception("Password reset email delivery failed")
