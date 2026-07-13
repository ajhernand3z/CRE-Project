"""Test fixtures: an isolated SQLite database loaded with the seed data.

CRE_DB_PATH must be set before any app module is imported, because the
engine is created at import time from that env var.
"""

import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="cre_test_")
os.environ["CRE_DB_PATH"] = os.path.join(_tmpdir, "test.db")

import pytest  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.ingest.seed import load_seed  # noqa: E402


@pytest.fixture(scope="session")
def seeded_db():
    init_db()
    session = SessionLocal()
    load_seed(session)
    session.close()
    return None


@pytest.fixture()
def db(seeded_db):
    session = SessionLocal()
    yield session
    session.close()
