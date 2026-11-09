from fastapi import HTTPException


class APIError(HTTPException):
    """HTTP error with a stable machine-readable code independent of its copy."""

    def __init__(self, status_code: int, detail: str, code: str, headers: dict | None = None):
        response_headers = dict(headers or {})
        response_headers["X-Error-Code"] = code
        self.code = code
        super().__init__(status_code=status_code, detail=detail, headers=response_headers)
