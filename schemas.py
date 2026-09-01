from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)

    # The verification email is sent via Gmail SMTP — restricting sign-ups
    # to @gmail.com keeps that simple and avoids delivery issues to other
    # providers before this has been tested more broadly.
    @field_validator("email")
    @classmethod
    def gmail_only(cls, v: str) -> str:
        if not v.lower().endswith("@gmail.com"):
            raise ValueError("Only Gmail addresses are supported")
        return v


class RegisterOut(BaseModel):
    message: str
    email: str


class VerifyCode(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ResendCode(BaseModel):
    email: EmailStr


class UserOut(BaseModel):
    id: int
    email: str | None = None
    username: str | None = None
    is_verified: bool
    created_at: datetime

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class QuotaOut(BaseModel):
    papers_downloaded_today: int
    papers_limit: int
    papers_left: int
    books_downloaded_today: int
    books_limit: int
    books_left: int


class PaperOut(BaseModel):
    id: str
    name: str
    category: str | None = None
    size_bytes: int | None = None
    # No download_url here on purpose: a permanent link in this list would
    # let anyone bypass the quota by copying it from the network tab.
    # Get a short-lived, quota-checked link from POST /drive/download/{id}.


class DownloadUrlOut(BaseModel):
    url: str
    expires_in: int
    quota: QuotaOut
