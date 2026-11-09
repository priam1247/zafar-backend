"""Small process-local rate limiter for the single-instance API deployment.

For multi-instance deployments, replace this store with Redis/another shared
counter so limits apply consistently across workers.
"""
from collections import defaultdict, deque
import threading
import time

from fastapi import HTTPException, Request

_buckets: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(key: str, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        bucket = _buckets[key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(bucket[0] + window_seconds - now + 0.999))
            raise HTTPException(
                status_code=429,
                detail="Too many requests — please try again later",
                headers={"Retry-After": str(retry_after), "X-Error-Code": "rate_limited"},
            )
        bucket.append(now)
        # Opportunistically prevent abandoned keys from accumulating forever.
        if len(_buckets) > 10000:
            stale = [k for k, values in _buckets.items() if not values or values[-1] <= cutoff]
            for stale_key in stale:
                _buckets.pop(stale_key, None)


def limit_request(
    request: Request,
    scope: str,
    identifier: str = "",
    *,
    limit: int,
    window: int,
    ip_limit: int | None = None,
) -> None:
    """Limit by IP and, when supplied, by account identifier.

    `ip_limit=None` keeps the IP ceiling equal to `limit`; set `ip_limit=0`
    for account-keyed flows where many legitimate users may share one NAT.
    """
    if ip_limit != 0:
        ip = client_ip(request)
        enforce_rate_limit(f"{scope}:ip:{ip}", limit if ip_limit is None else ip_limit, window)
    if identifier:
        enforce_rate_limit(f"{scope}:id:{identifier.strip().lower()}", limit, window)
