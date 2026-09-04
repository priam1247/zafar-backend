from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # No default on purpose: an unset JWT_SECRET_KEY should fail startup
    # loudly, not silently fall back to a committed value everyone can
    # read. (The previous default here was exposed — see SECURITY.md —
    # so this now requires a real secret to be set as an env var:
    #   python -c "import secrets; print(secrets.token_hex(32))"
    jwt_secret_key: str
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
    # A manual cache-buster already exists (POST /drive/refresh, called
    # after uploading new material) — so this only needs to cover
    # "eventually", not "within minutes". Hours, not seconds.
    drive_cache_ttl_seconds: int = 21600  # 6 hours

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
