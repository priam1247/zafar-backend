import base64
import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import Depends, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from errors import APIError
from models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=10)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

_credentials_error = APIError(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    code="bad_credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(user: User) -> str:
    issued_at = datetime.now(timezone.utc)
    expire = issued_at + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode(
        {
            "sub": user.username,
            "uid": user.id,
            "name": user.name,
            "email": user.email,
            "iat": issued_at.timestamp(),
            "exp": expire,
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        username = payload.get("sub")
        if username is None:
            raise _credentials_error
    except JWTError:
        raise _credentials_error

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise _credentials_error
    changed_at = user.password_changed_at
    if changed_at is not None:
        if changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=timezone.utc)
        issued_at = payload.get("iat")
        if not isinstance(issued_at, (int, float)) or float(issued_at) < changed_at.timestamp():
            raise _credentials_error
    return user


# ---------------------------------------------------------------------------
# Signed download URLs (verified by the download-proxy Cloudflare Worker)
# ---------------------------------------------------------------------------

def make_signed_download_url(file_id: str, file_name: str, source: str = "google") -> str:
    """Create a short-lived, source-bound Worker URL for a library file."""
    if not settings.download_proxy_base_url or not settings.download_signing_secret:
        raise APIError(status_code=500, detail="Download signing is not configured", code="download_signing_unavailable")
    if source not in {"google", "drime"}:
        raise APIError(status_code=500, detail="Download source is not supported", code="download_source_unavailable")

    expiry = int(time.time()) + settings.download_link_ttl_seconds
    msg = f"{source}.{file_id}.{expiry}".encode()
    sig = hmac.new(settings.download_signing_secret.encode(), msg, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    base = settings.download_proxy_base_url.rstrip("/")
    return f"{base}/dl/{source}/{quote(file_id, safe='')}?exp={expiry}&sig={sig_b64}&name={quote(file_name)}"
