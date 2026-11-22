"""The code-only oracle: all 22 files through the real graph with the hand-checked extractions in
tests/fixtures/extractions, a VP that approves everything and a critic that accepts everything. So every
status comes from code (grounding, validation, the guardrail and the ledger); tests/golden.json pins it on
a fresh DB per file and, for data/invoices, on one shared DB in sorted order (duplicates and revisions).
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from invoice_agent import db
from invoice_agent.graph import Deps, process_invoice
from invoice_agent.ingest import SUPPORTED_EXTENSIONS
from invoice_agent.log import RunLogger
from invoice_agent.models import Critique, Decision, Invoice, RunState

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests" / "golden.json").read_text())["code_only"]


def invoices(folder: str) -> list[Path]:
    return sorted(p for p in (ROOT / folder).iterdir() if p.suffix in SUPPORTED_EXTENSIONS)


SAMPLES = invoices("data/invoices")
FILES = SAMPLES + invoices("fixtures/invoices")


def extract(text: str, filename: str, problems: list[str]) -> tuple[Invoice, dict]:
    path = ROOT / "tests" / "fixtures" / "extractions" / f"{filename}.json"
    return Invoice.model_validate_json(path.read_text()), {"task": "extract"}


def approve_everything(invoice, validation, catalog, history) -> tuple[Decision, list, list[dict]]:
    return Decision(action="approve", rationale="The fixture VP approves everything.", concerns=[]), [], [{}]


def accept_everything(invoice, validation, catalog, decision, tool_calls) -> tuple[Critique, dict]:
    return Critique(verdict="accept", problems=[]), {}


def run(path: Path, db_file: Path) -> RunState:
    logger = RunLogger("20261120-000000-gold", runs_dir=db_file.parent / "runs", db_path=db_file)
    return process_invoice(path, Deps(extract, approve_everything, accept_everything, logger, db_file), "gold")


def test_golden_covers_every_file():
    assert len(SAMPLES) == 20 and [p.name for p in FILES] == list(GOLDEN)


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_each_file_on_a_fresh_db(path, tmp_path):
    db.seed(tmp_path / "inventory.db")
    state = run(path, tmp_path / "inventory.db")

    assert state.status == GOLDEN[path.name]["fresh"], state.reasons
    assert state.error is None and state.extract_attempts == 1  # every hand-checked extraction is grounded
    if state.validation.failures:  # the VP approved; the guardrail rejected it, citing every failure
        assert state.overridden and state.reasons == [c.message for c in state.validation.failures]
        assert state.critiques == []


def test_the_samples_in_order_on_one_db(tmp_path):
    db_file = tmp_path / "inventory.db"
    db.seed(db_file)
    states = {path.name: run(path, db_file) for path in SAMPLES}

    assert {name: s.status for name, s in states.items()} == {p.name: GOLDEN[p.name]["shared"] for p in SAMPLES}
    assert states["invoice_1011.txt"].payment_ref == states["invoice_1011.pdf"].payment_ref
    assert states["invoice_1012.txt"].payment_ref == states["invoice_1012.pdf"].payment_ref
    assert "already paid $1,890.00" in states["invoice_1004_revised.json"].reasons[0]
    with closing(sqlite3.connect(db_file)) as conn:
        numbers = [row[0] for row in conn.execute("SELECT invoice_number FROM payments")]
    assert len(numbers) == len(set(numbers)) == 7  # never one invoice number paid twice
