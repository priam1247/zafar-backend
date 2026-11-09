from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # No default on purpose: an unset JWT_SECRET_KEY should fail startup
    # loudly, not silently fall back to a committed value everyone can read.
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 10080  # 7 days

    # "d1" = Cloudflare D1, sqlite:///path/to/file.db = local SQLite, or Postgres.
    database_url: str = "sqlite:///./zafar.db"

    cloudflare_account_id: str = ""
    d1_database_id: str = ""
    cloudflare_api_token: str = ""

    # Credentials/configuration required for the deployed application to work.
    google_service_account_json: str = ""
    drive_root_folder_id: str = ""

    daily_paper_limit: int = 10
    daily_book_limit: int = 3

    download_proxy_base_url: str = ""
    download_signing_secret: str = ""
    download_link_ttl_seconds: int = 300
    drive_cache_ttl_seconds: int = 21600  # 6 hours

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""

    # Stored as CSV because the env contract is comma-separated; the public
    # allowed_origins property exposes the normalized list used by CORS.
    allowed_origins_csv: str = Field(
        default="https://library.zafarh.dpdns.org",
        validation_alias="ALLOWED_ORIGINS",
    )

    @property
    def allowed_origins(self) -> list[str]:
        origins = [origin.strip().rstrip("/") for origin in self.allowed_origins_csv.split(",") if origin.strip()]
        if not origins:
            raise ValueError("ALLOWED_ORIGINS must contain at least one origin")
        return origins

    @model_validator(mode="before")
    @classmethod
    def report_all_missing_required_settings(cls, values: Any) -> Any:
        values = values if isinstance(values, dict) else {}
        required_fields = {
            "JWT_SECRET_KEY": "jwt_secret_key",
            "DOWNLOAD_PROXY_BASE_URL": "download_proxy_base_url",
            "DOWNLOAD_SIGNING_SECRET": "download_signing_secret",
            "GOOGLE_SERVICE_ACCOUNT_JSON": "google_service_account_json",
            "DRIVE_ROOT_FOLDER_ID": "drive_root_folder_id",
            "SMTP_USER": "smtp_user",
            "SMTP_PASS": "smtp_pass",
        }
        missing = [name for name, field in required_fields.items() if not values.get(field)]
        if missing:
            raise ValueError("Missing required settings: " + ", ".join(missing))
        return values

    def model_post_init(self, __context: Any) -> None:
        required = {
            "JWT_SECRET_KEY": self.jwt_secret_key,
            "DOWNLOAD_PROXY_BASE_URL": self.download_proxy_base_url,
            "DOWNLOAD_SIGNING_SECRET": self.download_signing_secret,
            "GOOGLE_SERVICE_ACCOUNT_JSON": self.google_service_account_json,
            "DRIVE_ROOT_FOLDER_ID": self.drive_root_folder_id,
            "SMTP_USER": self.smtp_user,
            "SMTP_PASS": self.smtp_pass,
        }
        missing = [name for name, value in required.items() if not value or not value.strip()]
        if missing:
            raise ValueError("Missing required settings: " + ", ".join(missing))

    class Config:
        env_file = ".env"


settings = Settings()
