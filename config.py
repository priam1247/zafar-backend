from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # SECURITY: this default value is public (it shipped in a committed
    # file). Set a real JWT_SECRET_KEY env var on KataBump and rotate it —
    # do not rely on this default in production.
    jwt_secret_key: str = "ee8267019204b13955aa73a519eea4a7fd2fcad1146c56fd85e30a10671bd932"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 10080  # 7 days

    database_url: str = "sqlite:///./zafar.db"

    # Paste the full service_account.json content as this env var's value
    # (not a file path) — Koyeb's filesystem is ephemeral/container-only,
    # so there's no reliable place to keep an actual .json file on disk.
    google_service_account_json: str = ""
    drive_root_folder_id: str = "1SWv792HexEVx7tHaSBIZvgxKwgb6-lp4"

    daily_paper_limit: int = 10
    daily_book_limit: int = 3

    # Cloudflare Worker base URL, e.g. https://zafar-dl.yourname.workers.dev
    download_proxy_base_url: str = ""
    # Shared secret between backend and the download-proxy Worker for
    # signed download URLs. Set the SAME value in the Worker as env var
    # DL_SECRET. Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    download_signing_secret: str = ""
    # Signed links die after this many seconds.
    download_link_ttl_seconds: int = 300

    # How long the Drive folder tree is cached in memory (per folder).
    drive_cache_ttl_seconds: int = 600

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
