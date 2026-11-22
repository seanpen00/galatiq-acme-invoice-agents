"""Live end-to-end test against real Grok: ``main.py --json`` over both folders, one shared ledger each.

Opt-in and billed: ``pytest -m live -s`` (the default run deselects it); needs XAI_API_KEY in the environment
or .env. Code-decided outcomes must match tests/golden.json's replay statuses; the VP's judgment calls may
only be more cautious: a golden PAID may come back ESCALATED, and a DUPLICATE only follows a paid twin.
"""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from dotenv import dotenv_values

from invoice_agent.validate import invoice_key

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.live
# Only checked here, never loaded into this process: main.py reads .env itself.
if not (os.getenv("XAI_API_KEY") or dotenv_values(ROOT / ".env").get("XAI_API_KEY") or "").strip():
    pytest.skip("XAI_API_KEY is not set (live tests call Grok)", allow_module_level=True)

GOLDEN = json.loads((ROOT / "tests" / "golden.json").read_text())
_HOLDOUT_KEY = json.loads((ROOT / "tests" / "holdout.json").read_text())["expected"]
HOLDOUT = {name: e["status"] for name, e in _HOLDOUT_KEY.items()}
ALLOWED = {"REJECTED": {"REJECTED"}, "ESCALATED": {"ESCALATED"}, "PAID": {"PAID", "ESCALATED"}, "ERROR": {"ERROR"}}


@pytest.mark.parametrize(("folder", "golden"), [
    ("data/invoices", GOLDEN["replay_shared"]),
    ("fixtures/invoices", GOLDEN["replay_fixtures"]),
    ("fixtures/holdout", HOLDOUT),  # unseen invoices with an answer key (tests/holdout.json), never recorded
], ids=["samples", "fixtures", "holdout"])
def test_a_live_folder_run_is_at_least_as_cautious_as_golden(folder, golden, tmp_path):
    db_file = tmp_path / "inventory.db"
    env = {**os.environ, "LLM_MODE": "live", "XAI_MODEL": GOLDEN["model"], "INVENTORY_DB": str(db_file),
           "RUNS_DIR": str(tmp_path / "runs")}
    env.pop("XAI_API_BASE")  # conftest points it at a closed port; this test is the one meant to call Grok
    result = subprocess.run([sys.executable, "main.py", f"--invoice_path={folder}", "--json"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=900)
    rows = json.loads(result.stdout)

    problems, paid = [], set()
    for row in rows:
        name, status = Path(row["invoice_path"]).name, row["status"]
        number = invoice_key((row["invoice"] or {}).get("invoice_number"))  # twins may print it differently
        expected = golden[name]
        if expected == "DUPLICATE":  # a duplicate only once its twin was paid in this run; else judged as PAID
            allowed = {"DUPLICATE"} if number in paid else ALLOWED["PAID"]
        else:
            allowed = ALLOWED[expected]
        print(f"  {'ok ' if status in allowed else 'BAD'} {name}: {status} (golden {expected}) {row['reasons'][:1]}")
        if status not in allowed:
            problems.append(f"{name}: {status}, golden {expected}: {row['reasons']}")
        if status == "PAID":
            paid.add(number)
    assert not problems, "\n".join(problems)
    with closing(sqlite3.connect(db_file)) as conn:
        numbers = [r[0] for r in conn.execute("SELECT invoice_number FROM payments")]
    assert len(numbers) == len(set(numbers)), f"an invoice number was paid twice: {numbers}"
    assert result.returncode == (1 if "ERROR" in golden.values() else 0), result.stderr[-2000:]
