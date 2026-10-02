"""datapod: a shared SQLite database exposed as a remote MCP server."""

import base64
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

DB_PATH = Path(os.environ.get("DATAPOD_DB", "/data/datapod.db"))
MAX_ROWS = int(os.environ.get("DATAPOD_MAX_ROWS", "1000"))
QUERY_TIMEOUT = float(os.environ.get("DATAPOD_QUERY_TIMEOUT", "10"))

# Read-only introspection pragmas the AI may use; everything else is denied.
ALLOWED_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}


def _authorizer(action, arg1, arg2, db_name, trigger):
    # Keep every statement confined to the single main database file.
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_OK if arg1 and arg1.lower() in ALLOWED_PRAGMAS else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and arg2 and arg2.lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)  # autocommit
    deadline = time.monotonic() + QUERY_TIMEOUT
    # Returning non-zero aborts the running statement.
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    conn.set_authorizer(_authorizer)
    return conn


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")


def _jsonable(value):
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode()}
    return value


def _build_auth():
    if os.environ.get("DATAPOD_NO_AUTH") == "1":
        return None
    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    return OIDCProxy(
        config_url=f"https://{os.environ['AUTH_DOMAIN']}/.well-known/openid-configuration",
        client_id=os.environ["OIDC_CLIENT_ID"],
        client_secret=os.environ["OIDC_CLIENT_SECRET"],
        base_url=f"https://{os.environ['DOMAIN']}",
        jwt_signing_key=os.environ["JWT_SIGNING_KEY"],
        required_scopes=["openid", "profile", "email"],
        # Pocket ID's access tokens carry no scope claim, so verify the
        # id_token instead; scopes are still enforced on FastMCP's own tokens.
        verify_id_token=True,
        # OAuth client registrations and upstream tokens are stored encrypted
        # under FASTMCP_HOME (on the persistent /data volume).
    )


mcp = FastMCP(
    "datapod",
    instructions=(
        "A persistent, shared SQLite database. Use list_tables/describe_table to "
        "discover the schema, then execute_sql to read or write. Any SQL is allowed "
        "within this single database; ATTACH, extensions and most PRAGMAs are blocked."
    ),
    auth=_build_auth(),
)


@mcp.tool
def execute_sql(query: str, params: list | None = None) -> dict:
    """Execute a single SQL statement (SELECT, INSERT, CREATE TABLE, ...).

    Use `?` placeholders with `params` for values. SELECT results are capped
    at DATAPOD_MAX_ROWS rows.
    """
    with closing(_connect()) as conn:
        cur = conn.execute(query, params or [])
        if cur.description is None:
            return {"rowcount": cur.rowcount, "lastrowid": cur.lastrowid}
        rows = cur.fetchmany(MAX_ROWS + 1)
        return {
            "columns": [d[0] for d in cur.description],
            "rows": [[_jsonable(v) for v in row] for row in rows[:MAX_ROWS]],
            "truncated": len(rows) > MAX_ROWS,
        }


@mcp.tool
def list_tables() -> list[str]:
    """List the tables and views in the database."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_schema WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [r[0] for r in rows]


@mcp.tool
def describe_table(name: str) -> dict:
    """Show a table's CREATE statement and columns."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT sql FROM sqlite_schema WHERE name = ?", [name]).fetchone()
        if row is None:
            raise ValueError(f"No such table: {name}")
        cols = conn.execute("SELECT name, type, \"notnull\", dflt_value, pk FROM pragma_table_info(?)", [name])
        columns = [dict(zip(("name", "type", "notnull", "default", "pk"), c)) for c in cols]
    return {"sql": row[0], "columns": columns}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


if __name__ == "__main__":
    _init_db()
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
