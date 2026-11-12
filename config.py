from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

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
    # Established Zafar Materials (Sorted) Drive folder. Override with
    # DRIVE_ROOT_FOLDER_ID only when the library root is intentionally changed.
    drive_root_folder_id: str = "1SWv792HexEVx7tHaSBIZvgxKwgb6-lp4"

    daily_paper_limit: int = 10
    daily_book_limit: int = 3
    daily_notes_limit: int = 3
    daily_marking_keys_limit: int = 10
    email_daily_limit: int = 500
    email_daily_warn_threshold: int = 400

    # Operational settings
    api_docs_enabled: bool = False
    forwarded_allow_ips: str = "127.0.0.1"
    drive_max_depth: int = 6

    # Optional second library source. Credentials are set in deployment secrets.
    drime_api_key: str = ""
    drime_api_url: str = "https://app.drime.cloud/api/v1"
    drime_root_folder_id: str = "1016158780"
    drime_workspace_id: int = 0
    drime_papers_folder_names: str = "Mock,MANEB"
    drime_primary_folder_names: str = "Primary"
    drime_books_folder_names: str = "Books"
    drime_notes_folder_names: str = "Notes"
    drime_marking_keys_folder_names: str = "Marking Keys,Marking Schemes"

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
        default="https://studymw.netlibrary.dpdns.org",
        validation_alias="ALLOWED_ORIGINS",
    )

    @field_validator("forwarded_allow_ips")
    @classmethod
    def reject_untrusted_proxy_wildcard(cls, value: str) -> str:
        if any(part.strip() == "*" for part in value.split(",")):
            raise ValueError("FORWARDED_ALLOW_IPS must list trusted proxy IPs; wildcard is unsafe")
        return value

    @property
    def allowed_origins(self) -> list[str]:
        origins = [origin.strip().rstrip("/") for origin in self.allowed_origins_csv.split(",") if origin.strip()]
        if not origins:
            raise ValueError("ALLOWED_ORIGINS must contain at least one origin")
        for origin in origins:
            parsed = urlsplit(origin)
            local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if (
                "*" in origin
                or (parsed.scheme != "https" and not local_http)
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("ALLOWED_ORIGINS must contain exact HTTPS origins (HTTP is allowed only for localhost)")
            try:
                _ = parsed.port
            except ValueError as exc:
                raise ValueError("ALLOWED_ORIGINS contains an invalid port") from exc
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
            "SMTP_USER": "smtp_user",
            "SMTP_PASS": "smtp_pass",
        }
        missing = [
            name for name, field in required_fields.items()
            if values.get(field) is None or not str(values.get(field)).strip()
        ]
        if missing:
            raise ValueError("Missing required settings: " + ", ".join(missing))
        return values


settings = Settings()
