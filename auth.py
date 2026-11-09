import base64
import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=10)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

_credentials_error = HTTPException(          # module-level: built once, not per request
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(user: User) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode(
        {
            "sub": user.username,
            "uid": user.id,
            "name": user.name,
            "email": user.email,
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
    return user


# ---------------------------------------------------------------------------
# Signed download URLs (verified by the download-proxy Cloudflare Worker)
# ---------------------------------------------------------------------------

def make_signed_download_url(file_id: str, file_name: str) -> str:
    """
    Returns a Worker URL that expires after settings.download_link_ttl_seconds.
    Signature = HMAC-SHA256(secret, f"{file_id}.{expiry}") — the Worker
    recomputes and compares it, so links can't be forged or reused after expiry.

    Configuration is also validated at startup; this guard is defense in depth.
    """
    if not settings.download_proxy_base_url or not settings.download_signing_secret:
        raise HTTPException(status_code=500, detail="Download signing is not configured")

    expiry = int(time.time()) + settings.download_link_ttl_seconds
    msg = f"{file_id}.{expiry}".encode()
    sig = hmac.new(settings.download_signing_secret.encode(), msg, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    base = settings.download_proxy_base_url.rstrip("/")
    return f"{base}/dl/{file_id}?exp={expiry}&sig={sig_b64}&name={quote(file_name)}"
