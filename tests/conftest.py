"""Pytest fixtures.

We use an in-memory SQLite DB per test module via a dedicated engine, then
override FastAPI's `get_db` dependency to use it. The seed runs once at
session startup; all tests share the seeded reference data + demo users.
"""
from __future__ import annotations

import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Important: set BEFORE importing the app so settings pick up the test DB
os.environ["DATABASE_URL"] = "sqlite:///./test_jaratrade.db"
os.environ["JWT_SECRET"] = "test-secret-32-bytes-or-more-please-change"

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_default_data  # noqa: E402

TEST_DB_PATH = "./test_jaratrade.db"


@pytest.fixture(scope="session", autouse=True)
def _setup_database() -> Iterator[None]:
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    engine = create_engine(f"sqlite:///{TEST_DB_PATH}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with Session() as db:
        seed_default_data(db)

    def _override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    yield
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """slowapi keeps a process-wide counter; tests need a clean slate so login
    fixtures don't trip the 10/min limit after a few files have run."""
    try:
        from app.rate_limit import limiter
        limiter.reset()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def importer_token(client: TestClient) -> str:
    r = client.post("/imp/login", json={"email": "importer@jaratrade.com", "password": "REDACTED-old-default"})
    assert r.status_code == 200, r.text
    return r.json()["payload"]["token"]


@pytest.fixture
def exporter_token(client: TestClient) -> str:
    r = client.post("/exp/login", json={"email": "exporter@jaratrade.com", "password": "REDACTED-old-default"})
    assert r.status_code == 200, r.text
    return r.json()["payload"]["token"]


@pytest.fixture
def admin_token(client: TestClient) -> str:
    r = client.post("/adm/login", json={"email": "admin@jaratrade.com", "password": "REDACTED-old-default"})
    assert r.status_code == 200, r.text
    return r.json()["payload"]["token"]
