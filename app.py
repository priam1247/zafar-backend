import os
import uvicorn

from main import app  # noqa: F401 — re-exported so `python app.py` or `uvicorn app:app` both work

if __name__ == "__main__":
    # Most hosts (Koyeb included) set PORT themselves and route traffic to
    # it, so that's read first. KataBump's allocation forwards a fixed port
    # straight through with no PORT env var — 20208 stays as the fallback
    # for that case (update it if that allocation ever changes).
    port = int(os.environ.get("PORT", 20208))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        workers=1,             # >1 worker duplicates the whole process RAM
        access_log=False,      # real CPU savings under load
        limit_concurrency=50,  # shed load gracefully instead of OOMing
        timeout_keep_alive=15,
    )
