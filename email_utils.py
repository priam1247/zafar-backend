import smtplib
from email.mime.text import MIMEText

from config import settings


def send_code(email: str, code: str) -> None:
    """
    Emails the 6-digit verification code via Gmail SMTP.

    Run via FastAPI BackgroundTasks — never inline in a request, the Gmail
    SMTP handshake takes 1-3s.

    If smtp_user/smtp_pass aren't set (e.g. local dev, or before the
    KataBump env vars are configured), this just prints the code to the
    server console instead of raising — same "works with zero setup"
    philosophy as the rest of config.py's defaults.
    """
    if not settings.smtp_user or not settings.smtp_pass:
        print(f"\n[DEV] Verification code for {email}: {code}\n")
        return

    msg = MIMEText(
        f"Your Zafar verification code is: {code}\n\n"
        "This code expires in 10 minutes. If you didn't request this, "
        "you can ignore this email."
    )
    msg["Subject"] = "Your Zafar verification code"
    msg["From"] = settings.smtp_user
    msg["To"] = email

    try:
        # timeout: a Gmail hiccup must never pin a background thread forever
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_pass)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        # This runs as a background task — an exception here would
        # otherwise vanish silently instead of surfacing anywhere.
        print(f"[EMAIL ERROR] Could not send code to {email}: {exc}")
