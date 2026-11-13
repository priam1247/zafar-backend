"""Bootstrap the first owner or enroll an account's authenticator.

Usage (trusted terminal, with the production database environment loaded):
  python -m scripts.make_owner verified-user@gmail.com
  python -m scripts.make_owner --enroll verified-user@gmail.com

For hosted one-shot consoles that cannot provide stdin, add --non-interactive.

The first form assigns the first owner. The second only enrolls/re-enrolls TOTP;
an existing owner can then assign that account a role in the admin portal.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import func

import database
from admin_security import (
    encrypt_totp_secret,
    new_totp_secret,
    provisioning_uri,
    totp_step,
)
from database import SessionLocal
from models import AdminAuditLog, User


def enroll(email: str, *, bootstrap_owner: bool, confirm_totp: bool = True) -> None:
    normalized = email.strip().lower()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == normalized).first()
        if user is None or not user.is_verified:
            raise RuntimeError("The target must be an existing, verified account")
        if bootstrap_owner and db.query(func.count(User.id)).filter(User.role == "owner").scalar():
            raise RuntimeError("An owner already exists; use --enroll to prepare another admin")

        secret = new_totp_secret()
        print("Add this account to an authenticator app now:")
        print(provisioning_uri(secret, normalized))
        print("If scanning a QR is unavailable, enter this secret manually:")
        print(secret)
        if confirm_totp:
            code = input("Enter the current six-digit authenticator code to confirm enrollment: ").strip()
            step = totp_step(secret, code)
            if step is None:
                raise RuntimeError("Authenticator code was invalid; no role or secret was saved")
        else:
            # Some hosted one-shot consoles cannot provide stdin to an
            # interactive process. This mode is intentionally available only
            # to operators already trusted to execute this CLI on the service.
            step = 0

        encrypted = encrypt_totp_secret(secret)
        action = "owner.bootstrap" if bootstrap_owner else "admin.totp_enroll"
        if database.IS_D1:
            raw = database.engine.raw_connection()
            try:
                connection = raw.driver_connection
                update_sql = (
                    "UPDATE users SET role = ?, admin_totp_secret = ?, admin_totp_enabled = 1, "
                    "admin_totp_last_step = ? WHERE id = ? AND is_verified = 1 "
                    "AND (? = 0 OR NOT EXISTS (SELECT 1 FROM users WHERE role = 'owner'))"
                )
                audit_sql = (
                    "INSERT INTO admin_audit_log "
                    "(at, actor_user_id, actor_email, action, target, detail) "
                    "SELECT ?, ?, ?, ?, ?, ? WHERE changes() > 0"
                )
                role = "owner" if bootstrap_owner else user.role
                result = connection.execute_batch([
                    (update_sql, (role, encrypted, step, user.id, bootstrap_owner)),
                    (audit_sql, (datetime.now(timezone.utc), user.id, normalized, action,
                                 str(user.id), json.dumps({"role": role}, separators=(",", ":")))),
                ])
                if not (result[0].get("meta") or {}).get("changes"):
                    raise RuntimeError("Owner bootstrap condition changed; no role or authenticator was saved")
            finally:
                raw.close()
        else:
            if bootstrap_owner:
                user.role = "owner"
            user.admin_totp_secret = encrypted
            user.admin_totp_enabled = True
            user.admin_totp_last_step = step
            db.add(AdminAuditLog(
                actor_user_id=user.id,
                actor_email=normalized,
                action=action,
                target=str(user.id),
                detail=json.dumps({"role": user.role}, separators=(",", ":")),
            ))
            db.commit()
        message = "First owner enabled" if bootstrap_owner else "Authenticator enrolled"
        verification = "verified" if confirm_totp else "saved without interactive verification"
        print(f"{message} for {normalized}; TOTP {verification} and encrypted at rest.")
        if not confirm_totp:
            print("Add the displayed secret to your authenticator before signing in.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Securely bootstrap admin role and TOTP.")
    parser.add_argument("--enroll", action="store_true", help="only enroll TOTP; do not assign the owner role")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="skip the confirmation prompt for trusted hosted consoles without stdin",
    )
    parser.add_argument("email", help="existing verified account email")
    args = parser.parse_args()
    if args.non_interactive and not args.enroll:
        parser.error("--non-interactive can only be used with --enroll")
    try:
        enroll(args.email, bootstrap_owner=not args.enroll, confirm_totp=not args.non_interactive)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
