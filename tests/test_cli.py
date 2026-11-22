"""main.py as a real subprocess: friendly errors, pure --json with the run events, --reset-db and --workers.
Replay mode with an empty recordings folder makes every extraction a cache miss: an ERROR, offline."""

import json
import os
import subprocess
import sys
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_agent import db

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "invoices" / "invoice_1001.txt"


@pytest.fixture
def cli(tmp_path):
    (tmp_path / "no_recordings").mkdir()
    (tmp_path / "notes.docx").write_text("not an invoice")
    # conftest set LLM_MODE, RUNS_DIR and INVENTORY_DB; a blank key wins over .env (dotenv never overrides).
    env = {**os.environ, "LLM_CACHE_DIR": str(tmp_path / "no_recordings"), "XAI_API_KEY": "", "COLUMNS": "100"}

    def cli(*args: str, **overrides: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "main.py", *args], cwd=ROOT, env=env | overrides,
                              capture_output=True, text=True, timeout=120)

    return cli


@pytest.mark.parametrize(("path", "env", "message"), [
    ("{tmp}/nope.txt", {}, "File not found"),
    ("{tmp}/notes.docx", {}, "Unsupported file type '.docx'"),
    ("{tmp}", {}, "No invoice files"),
    (str(SAMPLE), {"LLM_MODE": "garbage"}, "LLM_MODE must be one of live, record or replay"),
    (str(SAMPLE), {"LLM_MODE": "live"}, "XAI_API_KEY is not set"),
    (str(SAMPLE), {"INVENTORY_DB": "{tmp}/notes.docx"}, "is not usable"),
], ids=["missing-file", "unsupported-file", "no-invoices", "bad-mode", "no-key", "not-a-db"])
def test_bad_input_is_a_friendly_error(cli, tmp_path, path, env, message):
    result = cli(f"--invoice_path={path.format(tmp=tmp_path)}", **{k: v.format(tmp=tmp_path) for k, v in env.items()})
    output = result.stdout + result.stderr
    assert result.returncode == 1 and message in output and "Traceback" not in output


def test_json_output_is_pure_json_and_the_run_is_logged(cli, tmp_path):
    row = json.loads(cli(f"--invoice_path={SAMPLE}", "--json", "--workers=2").stdout)
    assert row["status"] == "ERROR" and "No recorded Grok response" in row["error"] and "source_text" not in row
    [log] = (tmp_path / "runs").glob("*.jsonl")
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["stage"], e["message"]) for e in (events[0], events[-1])] == [("run", "started"), ("run", "finished")]
    assert (events[0]["mode"], events[0]["workers"], events[-1]["counts"]) == ("replay", 2, {"ERROR": 1})
    assert all(e["file"] == "invoice_1001.txt" for e in events[1:-1])

    rows = json.loads(cli("--invoice_path=fixtures/invoices", "--json").stdout)
    assert [Path(r["invoice_path"]).name for r in rows] == ["invoice_2001.txt", "invoice_2002.txt"]

    failed = json.loads(cli(f"--invoice_path={tmp_path}", "--json").stdout)  # an error before any invoice
    assert failed["status"] == "ERROR" and failed["error"].startswith("No invoice files")


def test_reset_db_reseeds_the_inventory_and_clears_the_ledger(cli, tmp_path):
    path = tmp_path / "inventory.db"
    db.seed(path)
    db.record_payment("INV-1001", "Widgets Inc.", Decimal("5000.00"), "USD", "PAY-1", path)

    result = cli("--reset-db", INVENTORY_DB=str(path))
    assert (result.returncode, result.stdout) == (0, "") and "Reset" in result.stderr
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
        assert conn.execute("SELECT stock FROM inventory WHERE item = 'WidgetA'").fetchone()[0] == 15


@pytest.mark.parametrize("workers", ["0", "17", "two"])
def test_workers_must_be_1_to_16(cli, workers):
    result = cli(f"--invoice_path={SAMPLE}", f"--workers={workers}")
    assert result.returncode == 2 and "--workers" in result.stderr and "Traceback" not in result.stderr
