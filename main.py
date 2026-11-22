"""Command line: python main.py --invoice_path=PATH [--json] [--workers N] [--reset-db]

Every file's first extraction is queued at once on a pool of --workers threads; the graph then runs one invoice
at a time on the main thread, in file order, so every ledger read and write happens as in a one-at-a-time run.
Exit code 0 when every invoice reached a business status, 1 on any ERROR or unusable input, 130 on Ctrl-C.
"""

import argparse
import dataclasses
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from invoice_agent import db, ingest, llm, ui
from invoice_agent.graph import Deps, default_deps, process_invoice
from invoice_agent.log import RunLogger, new_run_id
from invoice_agent.models import Invoice


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="main.py", description="Process invoices: extract, validate, review, pay.")
    parser.add_argument("--invoice_path", "--invoice-path", metavar="PATH", help="an invoice file or a folder of them")
    parser.add_argument("--json", action="store_true", help="print only JSON (a list for a folder)")
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=4, metavar="N", help="1-16 (default 4)")
    parser.add_argument("--reset-db", action="store_true", help="reseed the inventory DB, clearing the payment ledger")
    args = parser.parse_args(argv)
    if not args.invoice_path and not args.reset_db:
        parser.error("--invoice_path is required (or --reset-db on its own)")
    return args


def collect_files(path: Path) -> list[Path]:
    """A folder's supported files, sorted by name; anything else is one file (load reports a missing one)."""
    if not path.is_dir():
        return [path]
    files = sorted(p for p in path.iterdir() if p.suffix.lower() in ingest.SUPPORTED_EXTENSIONS)
    if not files:
        raise ingest.IngestError(f"No invoice files ({' '.join(sorted(ingest.SUPPORTED_EXTENSIONS))}) in {path}")
    return files


def prefetch(pool: ThreadPoolExecutor, path: Path, deps: Deps) -> Deps:
    """``deps`` for one file: its first extraction starts now on the pool, and the graph's first call returns it
    (or re-raises its error); retries call Grok directly. An unreadable file fails in the load node first."""
    first = pool.submit(lambda: ingest.extract(ingest.read_text(path), path.name))
    def extract(text: str, filename: str, problems: list[str]) -> tuple[Invoice, dict]:
        return ingest.extract(text, filename, problems) if problems else first.result()
    return dataclasses.replace(deps, extract=extract)


def run(args: argparse.Namespace, console: Console) -> int:
    db_file = db.db_path()
    if args.reset_db:
        db.seed(db_file)
        print(f"Reset {db_file}: inventory reseeded, payments and audit log cleared.", file=sys.stderr)
        if not args.invoice_path:
            return 0
    target = Path(args.invoice_path).expanduser()
    files = collect_files(target)
    llm.check_key()
    seeded = db.ensure_db(db_file)
    logger, started = RunLogger(new_run_id(), db_path=db_file), time.perf_counter()
    logger.event("run", "started", mode=llm.mode(), model=llm.model_name(), inventory_db=db_file, db_seeded=seeded,
                 invoice_path=target, files=len(files), workers=args.workers)
    ui.render_intro(console, mode=llm.mode(), model=llm.model_name(), run_id=logger.run_id, count=len(files),
                    db_path=f"{db_file} (created and seeded)" if seeded else str(db_file), workers=args.workers)
    deps, states = default_deps(logger, db_file), []
    pool = ThreadPoolExecutor(max_workers=args.workers)
    try:
        prefetched = [prefetch(pool, path, deps) for path in files]  # queued in file order
        for index, (path, file_deps) in enumerate(zip(files, prefetched), start=1):
            with ui.stage_progress(console, path.name, index, len(files)) as on_stage:
                states.append(process_invoice(path, file_deps, logger.run_id, on_stage))
            ui.render_result(states[-1], console)
            data = json.dumps(ui.result_to_dict(states[-1]), ensure_ascii=False)
            db.save_result(str(path), logger.run_id, states[-1].status, data, db_file)  # for the dashboard
    finally:  # after an error, queued extractions are dropped instead of calling Grok for nothing
        pool.shutdown(wait=False, cancel_futures=True)
    counts = Counter(state.status for state in states)
    logger.event("run", "finished", counts=counts, duration_ms=round((time.perf_counter() - started) * 1000, 1))
    if args.json:
        rows = [ui.result_to_dict(state) for state in states]
        print(json.dumps(rows if target.is_dir() else rows[0], indent=2, ensure_ascii=False))
    if target.is_dir():
        ui.render_summary(states, console)
    console.print(f"\nRun log: {logger.path}", style="dim", markup=False, soft_wrap=True)
    return 1 if counts["ERROR"] else 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env")  # never overrides variables already set
    args = parse_args(argv)
    try:
        return run(args, Console(quiet=args.json))  # --json: nothing but the JSON on stdout
    except KeyboardInterrupt:  # pool threads may be mid Grok call: exit now instead of waiting for them
        print("Interrupted.", file=sys.stderr, flush=True)
        os._exit(130)
    except Exception as exc:  # noqa: BLE001 - the CLI never shows a traceback
        message = str(exc) if isinstance(exc, (ingest.IngestError, llm.LLMError)) else f"{type(exc).__name__}: {exc}"
        if isinstance(exc, sqlite3.Error):  # the graph catches its own, so this is the inventory DB's setup
            message = f"Inventory DB {db.db_path()} is not usable ({exc}). Set INVENTORY_DB to another path."
        if args.json:
            print(json.dumps({"status": "ERROR", "error": message}), flush=True)
        print(f"Error: {message}", file=sys.stderr, flush=True)
        os._exit(1)  # as for Ctrl-C: don't wait for prefetches still calling Grok


if __name__ == "__main__":
    sys.exit(main())
