"""Shared test setup: offline by default, with the inventory DB and run logs under each test's tmp_path."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODE", "replay")  # never the network unless a test says otherwise
    monkeypatch.setenv("XAI_API_BASE", "http://127.0.0.1:9/v1/")  # and a stray live call fails at a closed port
    monkeypatch.setenv("LANGSMITH_TRACING", "false")  # also wins over .env in CLI subprocesses: no traces sent
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("INVENTORY_DB", str(tmp_path / "default.db"))


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A freshly seeded inventory DB, also set as INVENTORY_DB."""
    from invoice_agent import db

    path = tmp_path / "inventory.db"
    monkeypatch.setenv("INVENTORY_DB", str(path))
    db.seed(path)
    return path
