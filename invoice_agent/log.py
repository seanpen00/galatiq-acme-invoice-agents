"""Run logging: each event goes to ``runs/<run_id>.jsonl`` and to the ``audit_log`` table.

A failed write is a ``logging.warning``, never an exception, so logging cannot break a run.
"""

import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

from pydantic_core import to_jsonable_python  # pydantic models as dicts; Decimals and paths as text

from invoice_agent import db


def new_run_id() -> str:
    """A sortable, readable run id such as ``20261116-231502-3f9a`` (local time + random hex)."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class RunLogger:
    """One run's events. ``bind`` returns a copy that adds fields (e.g. the file name) to every event."""

    def __init__(self, run_id: str, runs_dir: db.PathArg = None, db_path: db.PathArg = None) -> None:
        self.run_id, self.db_path, self.context = run_id, db_path, {}
        self.path = (Path(runs_dir) if runs_dir else db.repo_path(os.getenv("RUNS_DIR") or "runs")) / f"{run_id}.jsonl"

    def bind(self, **context: object) -> "RunLogger":
        child = RunLogger(self.run_id, self.path.parent, self.db_path)
        child.context = {**self.context, **context}
        return child

    def event(self, stage: str, message: str, *, invoice_number: str | None = None, level: str = "info",
              **data: object) -> None:
        """Record one event; extra keyword arguments become fields of the JSON line and the audit row."""
        data = {**self.context, **data}
        record = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), "run_id": self.run_id,
                  "invoice_number": invoice_number, "stage": stage, "level": level, "message": message, **data}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=to_jsonable_python, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - logging must never break a run
            logging.warning("Could not write the run log %s: %s", self.path, exc)
        try:
            data_json = json.dumps(data, default=to_jsonable_python, ensure_ascii=False)
            db.log_audit(self.run_id, invoice_number, stage, level, message, data_json, path=self.db_path)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Could not write the audit log: %s", exc)
