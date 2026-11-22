"""SQLite storage: the mock inventory catalog, the payment ledger and the audit log.

Every function opens its own short-lived connection. Money is stored as TEXT (``str(Decimal)``)
and read back as ``Decimal``, so it never passes through a float. The database is ``INVENTORY_DB``
(default ``inventory.db``); a relative path is taken from the repo root, so running from another
directory still uses the same ledger.
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

PathArg = str | os.PathLike[str] | None

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1  # PRAGMA user_version of a database this code created

# (item, stock, unit_price): the brief's stock plus catalog prices. WidgetC, SuperGizmo and
# MegaSprocket are deliberately absent.
SEED_INVENTORY: list[tuple[str, int, str | None]] = [
    ("WidgetA", 15, "250.00"),
    ("WidgetB", 10, "500.00"),
    ("GadgetX", 5, "750.00"),
    ("FakeItem", 0, None),
]

RESULTS_TABLE = """
CREATE TABLE IF NOT EXISTS results (
    invoice_path TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""  # the latest result per invoice file, for the review dashboard

SCHEMA = """
DROP TABLE IF EXISTS results;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS inventory;
CREATE TABLE inventory (
    item TEXT PRIMARY KEY,
    stock INTEGER NOT NULL CHECK (stock >= 0),
    unit_price TEXT
);
CREATE TABLE payments (
    id INTEGER PRIMARY KEY,
    invoice_number TEXT NOT NULL,
    vendor TEXT,
    amount TEXT NOT NULL,
    currency TEXT,
    reference TEXT UNIQUE,
    paid_at TEXT NOT NULL
);
CREATE INDEX idx_payments_invoice_number ON payments (invoice_number);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    invoice_number TEXT,
    stage TEXT,
    level TEXT,
    message TEXT,
    data_json TEXT,
    created_at TEXT
);
CREATE INDEX idx_audit_log_run_id ON audit_log (run_id);
""" + RESULTS_TABLE


def repo_path(path: str | os.PathLike[str]) -> Path:
    """A configured path: absolute as given, relative to the repo root otherwise (never the cwd)."""
    resolved = Path(path).expanduser()
    return resolved if resolved.is_absolute() else REPO_ROOT / resolved


def db_path(path: PathArg = None) -> Path:
    """An explicit ``path`` as given, else ``$INVENTORY_DB`` or ``inventory.db`` from the repo root."""
    if path is not None:
        return Path(path).expanduser()
    return repo_path(os.getenv("INVENTORY_DB") or "inventory.db")


def connect(path: PathArg = None) -> sqlite3.Connection:
    """A new connection with ``sqlite3.Row`` rows; the caller closes it."""
    conn = sqlite3.connect(db_path(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _session(path: PathArg) -> Iterator[sqlite3.Connection]:
    """A connection that commits on success, rolls back on error and always closes."""
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def seed(path: PathArg = None) -> None:
    """Drop and recreate every table with the seed inventory, empty payments and an empty audit log."""
    target = db_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _session(target) as conn:  # one transaction: a failed reseed leaves the old database intact
        conn.executescript("BEGIN;" + SCHEMA)
        conn.executemany("INSERT INTO inventory VALUES (?, ?, ?)", SEED_INVENTORY)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def ensure_db(path: PathArg = None) -> bool:
    """Seed the database when the file is missing or was not made by this code; True if it seeded.

    A current database is left alone, so the payment history survives between runs.
    """
    target = db_path(path)
    if target.is_file():
        with _session(target) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
                conn.execute(RESULTS_TABLE)  # a database made before the dashboard existed gains the table
                return False
    seed(target)
    return True


def get_catalog(path: PathArg = None) -> dict[str, dict[str, Any]]:
    """Every inventory item: ``{item: {"stock": int, "unit_price": Decimal | None}}``."""
    with _session(path) as conn:
        rows = conn.execute("SELECT item, stock, unit_price FROM inventory ORDER BY rowid").fetchall()
    return {
        row["item"]: {
            "stock": row["stock"],
            "unit_price": None if row["unit_price"] is None else Decimal(row["unit_price"]),
        }
        for row in rows
    }


def find_payment(invoice_number: str, path: PathArg = None) -> dict[str, Any] | None:
    """The latest payment recorded for an invoice-number key (amount as Decimal), or None."""
    with _session(path) as conn:
        row = conn.execute(
            "SELECT invoice_number, vendor, amount, currency, reference, paid_at FROM payments "
            "WHERE invoice_number = ? ORDER BY id DESC LIMIT 1",
            (invoice_number,),
        ).fetchone()
    return None if row is None else {**dict(row), "amount": Decimal(row["amount"])}


def record_payment(
    invoice_number: str, vendor: str, amount: Decimal, currency: str, reference: str, path: PathArg = None
) -> None:
    """Append one payment to the ledger."""
    with _session(path) as conn:
        conn.execute(
            "INSERT INTO payments (invoice_number, vendor, amount, currency, reference, paid_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (invoice_number, vendor, str(amount), currency, reference, _now()),
        )


def save_result(invoice_path: str, run_id: str, status: str, data_json: str, path: PathArg = None) -> None:
    """Keep the latest result for one invoice file (``ui.result_to_dict`` as JSON)."""
    with _session(path) as conn:
        conn.execute("INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?)",
                     (invoice_path, run_id, status, data_json, _now()))


def get_results(path: PathArg = None) -> list[sqlite3.Row]:
    """Every saved result, by file path."""
    with _session(path) as conn:
        return conn.execute("SELECT * FROM results ORDER BY invoice_path").fetchall()


def log_audit(
    run_id: str, invoice_number: str | None, stage: str, level: str, message: str, data_json: str, path: PathArg = None
) -> None:
    """Append one event to ``audit_log``; ``data_json`` is the event's extra fields as JSON."""
    with _session(path) as conn:
        conn.execute(
            "INSERT INTO audit_log (run_id, invoice_number, stage, level, message, data_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, invoice_number, stage, level, message, data_json, _now()),
        )
