"""
Shared pytest fixtures for the media-library test suite.

The main challenge this file solves: app/main.py runs side effects at *import
time* — it reads DB_PATH from the environment, calls init_db() to create the
schema, and mounts a StaticFiles directory. So the environment has to be set up
BEFORE main is ever imported. We do that here, once, at module load, and expose
the app plus a fresh-database fixture to the tests.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

# app/ is a sibling of tests/; put it on the path so `import main` works.
APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

# A throwaway static dir so main.py's app.mount() doesn't fail in CI (no /static there).
_STATIC_TMP = tempfile.mkdtemp(prefix="media-library-static-")
os.environ["STATIC_DIR"] = _STATIC_TMP

# A per-session temp database, set before main is imported so init_db() targets it.
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="media-library-test-", suffix=".db")
os.close(_DB_FD)
os.environ["DB_PATH"] = _DB_PATH

# Now it's safe to import the app module.
import main  # noqa: E402


@pytest.fixture
def fresh_db():
    """
    Give each test a clean database: wipe the media table and re-run init_db()
    so schema/indexes are present, without re-importing main. Yields the sqlite3
    connection helper module-level DB_PATH so tests can inspect rows directly.
    """
    # Clear all rows between tests for isolation.
    conn = main._connect()
    conn.execute("DELETE FROM media")
    conn.commit()
    conn.close()
    yield main.DB_PATH


@pytest.fixture
def client(fresh_db):
    """A FastAPI TestClient bound to the app, with a fresh DB per test."""
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c
