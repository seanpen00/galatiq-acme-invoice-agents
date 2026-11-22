"""Replay golden test: ``main.py --json`` over both folders, every Grok call served from the recordings in
fixtures/llm_cache (never the network), must give tests/golden.json's replay_shared and replay_fixtures.
The recordings come from a reviewed LLM_MODE=record run; re-record them whenever a prompt or schema changes.
"""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests" / "golden.json").read_text())


@pytest.mark.parametrize(("folder", "expected"), [
    ("data/invoices", GOLDEN["replay_shared"]),
    ("fixtures/invoices", GOLDEN["replay_fixtures"]),
], ids=["samples", "fixtures"])
def test_a_replayed_folder_run_matches_golden(folder, expected, tmp_path):
    db_file = tmp_path / "inventory.db"
    env = {**os.environ, "LLM_MODE": "replay", "LLM_CACHE_DIR": str(ROOT / "fixtures" / "llm_cache"),
           "XAI_MODEL": GOLDEN["model"], "XAI_API_KEY": "", "INVENTORY_DB": str(db_file)}  # the model is in the key
    result = subprocess.run([sys.executable, "main.py", f"--invoice_path={folder}", "--json"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=300)
    rows = json.loads(result.stdout)

    errors = [row["error"] for row in rows if row["error"]]
    assert {Path(row["invoice_path"]).name: row["status"] for row in rows} == expected, errors[:3]
    assert all(call["mode"] == "replay" and call["cached"] for row in rows for call in row["llm_calls"])
    assert result.returncode == 0, result.stderr[-2000:]
    with closing(sqlite3.connect(db_file)) as conn:
        numbers = [row[0] for row in conn.execute("SELECT invoice_number FROM payments")]
    assert len(numbers) == len(set(numbers))  # never one invoice number paid twice
