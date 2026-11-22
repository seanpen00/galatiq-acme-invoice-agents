"""The review dashboard, driven headless with Streamlit's AppTest: approve pays once through payment.pay
(so EUR is still refused), reject pays nothing, and every decision is saved and logged."""

import json
from contextlib import closing
from pathlib import Path

from streamlit.testing.v1 import AppTest

from invoice_agent import db, ui
from invoice_agent.graph import Deps, process_invoice
from invoice_agent.log import RunLogger
from invoice_agent.models import Critique, Decision, Invoice

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "20261121-000000-dash"


def escalated(name: str, db_path: Path) -> str:
    """Run one sample with its hand-checked extraction and a VP that escalates; save it as main.py does."""
    path = ROOT / "data" / "invoices" / name
    extraction = Invoice.model_validate_json((ROOT / "tests" / "fixtures" / "extractions" / f"{name}.json").read_text())
    deps = Deps(
        extract=lambda text, filename, problems: (extraction, {}),
        approve=lambda *args: (Decision(action="escalate", rationale="Needs a person.", concerns=[]), [], [{}]),
        critic=lambda *args: (Critique(verdict="accept", problems=[]), {}),
        logger=RunLogger(RUN_ID, db_path=db_path),
        db_path=db_path,
    )
    state = process_invoice(path, deps, RUN_ID)
    assert state.status == "ESCALATED"
    db.save_result(str(path), RUN_ID, state.status, json.dumps(ui.result_to_dict(state)), db_path)
    return str(path)


def statuses(db_path: Path) -> dict[str, str]:
    return {Path(row["invoice_path"]).name: row["status"] for row in db.get_results(db_path)}


def test_reviewer_decisions_go_through_the_payment_rules(db_path):
    usd, eur = escalated("invoice_1004.json", db_path), escalated("invoice_1014.xml", db_path)
    app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=30).run()
    assert not app.exception and app.subheader[0].value == "Needs review (2)"
    shown = " ".join(e.value for e in [*app.markdown, *app.caption, *app.warning])
    for detail in ("Why it's held", "742 Evergreen Terrace", "✓ Vendor", "VP review", "Needs a person"):
        assert detail in shown, detail  # the card's details, incl. the address behind 1004's escalation
    assert len(app.dataframe) == 3  # each escalated card's line items, plus the all-invoices table

    app.button(key=f"approve:{usd}").click().run()  # USD: paid once
    app.button(key=f"approve:{eur}").click().run()  # EUR: pay() still refuses, so it stays in the queue
    assert statuses(db_path) == {"invoice_1004.json": "PAID", "invoice_1014.xml": "ESCALATED"}
    assert "USD only" in app.success[0].value and app.subheader[0].value == "Needs review (1)"

    app.button(key=f"reject:{eur}").click().run()
    assert statuses(db_path) == {"invoice_1004.json": "PAID", "invoice_1014.xml": "REJECTED"}
    with closing(db.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
        reviews = conn.execute("SELECT data_json FROM audit_log WHERE stage = 'review'").fetchall()
    assert [json.loads(r[0])["action"] for r in reviews] == ["approve", "approve", "reject"]


def test_empty_dashboard_says_what_to_run(db_path):
    app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=30).run()
    assert not app.exception and "python main.py" in app.info[0].value
