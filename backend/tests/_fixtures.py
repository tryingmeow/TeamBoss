"""Shared test plumbing: a throwaway database per test case, and inline stand-ins.

Not a test module (the name does not match test*.py), so neither pytest nor
unittest discovery collects it. Topic fixtures live next to it in
_patrol_fixtures.py and _seat_fixtures.py.
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database


async def direct_call(func, *args, **kwargs):
    """Stand-in for run_chatgpt_call: calls func inline, with no thread pool, rate limit or token refresh."""
    return func(*args, **kwargs)


def temp_db_dir(case) -> str:
    """Point app.database at a new temp directory for the rest of ``case``. Creates no schema.

    Returns the directory path. Cleanups run in reverse order: the patch stops
    first, then the directory is removed.
    """
    tmpdir = tempfile.TemporaryDirectory()
    case.addCleanup(tmpdir.cleanup)
    patcher = patch.object(app_database, "get_db_dir", return_value=tmpdir.name)
    patcher.start()
    case.addCleanup(patcher.stop)
    return tmpdir.name


def _checked_db_path(db_dir: str) -> str:
    db_path = app_database.get_db_path()
    if not db_path.startswith(db_dir + os.sep):
        raise AssertionError(f"test database {db_path!r} is outside its temp dir {db_dir!r}")
    return db_path


def start_temp_db(case) -> str:
    """temp_db_dir() plus the real init_database(), so the schema matches production.

    Returns the database file path, which is checked to be inside the temp dir.
    For a test that already runs an event loop, use start_temp_db_async().
    """
    db_dir = temp_db_dir(case)
    asyncio.run(app_database.init_database())
    return _checked_db_path(db_dir)


async def start_temp_db_async(case) -> str:
    """start_temp_db() for asyncSetUp, where an event loop is already running."""
    db_dir = temp_db_dir(case)
    await app_database.init_database()
    return _checked_db_path(db_dir)


def insert_row(conn, table: str, row: dict):
    """INSERT one ``{column: value}`` row. Returns the cursor; the caller commits."""
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    return conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))
