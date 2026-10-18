"""
DB-API 2.0 driver that runs SQL against Cloudflare D1 over its REST API.

SQLAlchemy's SQLite dialect is used unchanged on top of this module, so the
models, queries and migrations in this app stay as they are. D1 is SQLite,
so the SQL SQLAlchemy generates runs as-is.

What this driver does NOT give you:
  * Interactive transactions. D1's REST API has no BEGIN ... COMMIT across
    requests. Each statement commits on its own. executemany() uses D1's
    batch endpoint, which is atomic for that one batch. rollback() cannot
    undo writes that already happened; it logs a warning when it is called
    after writes.
  * Low latency. Every statement is one HTTPS request to Cloudflare.

Config (read by database.py when DATABASE_URL=d1):
    CLOUDFLARE_ACCOUNT_ID, D1_DATABASE_ID, CLOUDFLARE_API_TOKEN
    The token needs D1 Read and D1 Write permission for this database.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import sys
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

apilevel = "2.0"
threadsafety = 1
paramstyle = "qmark"

# SQLAlchemy reads this to decide which SQLite features to use. Report a
# version older than RETURNING support (SQLite 3.35), so SQLAlchemy uses
# cursor.lastrowid for inserts, which D1 reports as meta.last_row_id.
sqlite_version_info = (3, 34, 1)
sqlite_version = "3.34.1"

BASE_URL = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}"
    "/d1/database/{database_id}/query"
)


# --- DB-API exception hierarchy -------------------------------------------

class Warning(Exception):  # noqa: A001 — name required by the DB-API spec
    pass


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


_CONSTRAINT_HINTS = ("constraint failed",)


def _classify(message: str) -> type:
    lowered = message.lower()
    if any(hint in lowered for hint in _CONSTRAINT_HINTS):
        return IntegrityError
    if "syntax error" in lowered:
        return ProgrammingError
    return OperationalError


def _error_from_payload(payload: dict) -> Exception:
    messages = [
        e.get("message", "") for e in (payload.get("errors") or []) if isinstance(e, dict)
    ]
    message = "; ".join(m for m in messages if m) or "D1 request failed"
    return _classify(message)(message)


# --- Transports -----------------------------------------------------------

class _Transport:
    """Sends one request body and returns the parsed D1 response payload."""

    def post(self, body: dict) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    def send(self, body: dict) -> list:
        """Run a single query or a batch and return per-statement results."""
        payload = self.post(body)
        if not isinstance(payload, dict) or not payload.get("success"):
            raise _error_from_payload(payload if isinstance(payload, dict) else {})
        results = payload.get("result") or []
        for result in results:
            if not result.get("success", True):
                raise OperationalError("D1 statement failed")
        return results


class D1HTTPTransport(_Transport):
    """Calls Cloudflare's D1 query endpoint with a bearer token."""

    def __init__(
        self,
        account_id: str | None,
        database_id: str | None,
        api_token: str | None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
    ):
        self._account_id = account_id or ""
        self._database_id = database_id or ""
        self._api_token = api_token or ""
        self._url = base_url.format(account_id=self._account_id, database_id=self._database_id)
        self._timeout = timeout

    def post(self, body: dict) -> dict:
        if not (self._account_id and self._database_id and self._api_token):
            raise InterfaceError(
                "D1 is not configured: set CLOUDFLARE_ACCOUNT_ID, D1_DATABASE_ID "
                "and CLOUDFLARE_API_TOKEN"
            )
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Cloudflare returns JSON error bodies (bad token, bad SQL, ...).
            try:
                return json.loads(exc.read())
            except ValueError as parse_exc:
                raise OperationalError(f"D1 returned HTTP {exc.code}") from parse_exc
        except OSError as exc:  # URLError, timeouts, connection refused
            raise OperationalError(f"D1 unreachable: {exc}") from exc

        try:
            return json.loads(raw)
        except ValueError as exc:
            raise OperationalError("D1 returned invalid JSON") from exc


# --- Connection and cursor ------------------------------------------------

_TXN_WORDS = ("BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE")

# PRAGMAs that D1 refuses ("not authorized: SQLITE_AUTH") but that SQLAlchemy
# runs by itself. Answered locally with the value D1 actually has.
# PRAGMA table_xinfo(...) (with optional "schema." prefix), rewritten to table_info.
_TABLE_XINFO = re.compile(
    r"^\s*PRAGMA\s+((?:\w+\.)?)table_xinfo\s*\(\s*(.*?)\s*\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)

_LOCAL_PRAGMAS = {
    # 0 = READ COMMITTED semantics, which is all D1 offers.
    re.compile(r"^\s*PRAGMA\s+read_uncommitted\s*;?\s*$", re.IGNORECASE): ("read_uncommitted", 0),
}


def _is_transaction_control(sql: str) -> bool:
    parts = sql.strip().split(None, 1)
    return bool(parts) and parts[0].rstrip(";").upper() in _TXN_WORDS


def _convert_param(value):
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bool):  # bool is a subclass of int; keep it explicit
        return int(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat(sep=" ")
    raise ProgrammingError(f"unsupported parameter type for D1: {type(value).__name__}")


class Connection:
    def __init__(self, transport: _Transport):
        self._transport = transport
        self.closed = False
        self._writes = 0  # statements that changed rows since the last commit

    def cursor(self) -> "Cursor":
        self._ensure_open()
        return Cursor(self)

    def commit(self) -> None:
        # Every statement is already committed by D1.
        self._writes = 0

    def rollback(self) -> None:
        if self._writes:
            log.warning(
                "rollback() after %d write(s): D1 has already committed them", self._writes
            )
        self._writes = 0

    def close(self) -> None:
        self.closed = True

    def create_function(self, *args, **kwargs) -> None:
        # SQLAlchemy registers helper SQL functions (e.g. REGEXP) on each
        # connection. They can't be registered on D1, and this app doesn't
        # use them, so this is a deliberate no-op.
        return None

    def _ensure_open(self) -> None:
        if self.closed:
            raise InterfaceError("connection is closed")


class Cursor:
    arraysize = 1

    def __init__(self, connection: Connection):
        self._connection = connection
        self.description = None
        self.rowcount = -1
        self.lastrowid = None
        self.closed = False
        self._rows: list = []
        self._pos = 0

    def _ensure_open(self) -> None:
        if self.closed:
            raise InterfaceError("cursor is closed")
        self._connection._ensure_open()

    def _reset(self) -> None:
        self.description = None
        self.rowcount = -1
        self.lastrowid = None
        self._rows = []
        self._pos = 0

    def _set_local_row(self, column: str, value) -> None:
        self._reset()
        self.description = ((column, None, None, None, None, None, None),)
        self._rows = [(value,)]
        self.rowcount = -1

    def _apply(self, result: dict) -> None:
        meta = result.get("meta") or {}
        rows = result.get("results") or []
        changes = meta.get("changes") or 0
        self.rowcount = changes
        self.lastrowid = meta.get("last_row_id")
        if changes > 0:
            self._connection._writes += 1
        if rows:
            columns = list(rows[0].keys())
            self.description = tuple((c, None, None, None, None, None, None) for c in columns)
            self._rows = [tuple(row.get(c) for c in columns) for row in rows]
        else:
            self.description = None
            self._rows = []
        self._pos = 0

    def execute(self, operation: str, parameters=None):
        self._ensure_open()
        if _is_transaction_control(operation):
            self._reset()
            return self
        for pattern, (column, value) in _LOCAL_PRAGMAS.items():
            if pattern.match(operation):
                self._set_local_row(column, value)
                return self
        xinfo = _TABLE_XINFO.match(operation)
        if xinfo:
            return self._execute_table_xinfo(xinfo.group(1), xinfo.group(2))
        params = [_convert_param(p) for p in (parameters or ())]
        results = self._connection._transport.send({"sql": operation, "params": params})
        if results:
            self._apply(results[0])
        else:
            self._reset()
        return self

    def _execute_table_xinfo(self, schema: str, table: str):
        """SQLAlchemy 2.x reads column info with PRAGMA table_xinfo, which D1 may
        refuse. Send PRAGMA table_info instead and add the extra 'hidden' column
        (always 0 here: D1 has no hidden or generated columns in our schema)."""
        self.execute(f"PRAGMA {schema}table_info({table})")
        if self._rows:
            self.description = tuple(self.description) + (("hidden", None, None, None, None, None, None),)
            self._rows = [tuple(row) + (0,) for row in self._rows]
        return self

    def executemany(self, operation: str, seq_of_parameters):
        self._ensure_open()
        if _is_transaction_control(operation):
            self._reset()
            return self
        statements = [
            {"sql": operation, "params": [_convert_param(p) for p in params]}
            for params in seq_of_parameters
        ]
        if not statements:
            self._reset()
            return self
        results = self._connection._transport.send({"batch": statements})
        total = sum(max((r.get("meta") or {}).get("changes") or 0, 0) for r in results)
        if results:
            self._apply(results[-1])
        self.rowcount = total
        return self

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int | None = None):
        size = size or self.arraysize
        out = self._rows[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def fetchall(self):
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def __iter__(self):
        return iter(self.fetchall())

    def close(self) -> None:
        self.closed = True
        self._rows = []

    def setinputsizes(self, sizes) -> None:
        return None

    def setoutputsize(self, size, column=None) -> None:
        return None


def connect(transport: _Transport | None = None, *, account_id=None, database_id=None, api_token=None) -> Connection:
    """DB-API entry point. Pass `transport` in tests; otherwise uses the HTTP API."""
    if transport is None:
        transport = D1HTTPTransport(account_id, database_id, api_token)
    return Connection(transport)


def make_engine(transport: _Transport):
    """Build a SQLAlchemy engine whose SQL goes to D1 through this driver."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    # Connections are stateless (each statement is an HTTP request), so a
    # pool buys nothing. NullPool avoids keeping any connection state around.
    return create_engine(
        "sqlite://",
        module=sys.modules[__name__],
        creator=lambda: connect(transport=transport),
        poolclass=NullPool,
    )
