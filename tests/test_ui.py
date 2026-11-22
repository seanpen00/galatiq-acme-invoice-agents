"""Rendering tests: an 80-column card with two VP rounds, markup safety, the folder summary, --json and progress."""

import io
import json

from rich.console import Console

from invoice_agent import ui
from invoice_agent.models import Critique, Decision, Invoice, RunState, ToolCall, ValidationReport

SHORT = {"code": "INSUFFICIENT_STOCK", "passed": False, "message": "GadgetX: 20 requested, only 5 in stock"}
LINE = {"item": "GadgetX", "quantity": 20, "unit_price": 750, "line_total": None, "note": None, "sku": "GadgetX"}
STOCK = {"sku": "GadgetX", "requested": 20, "in_stock": 5, "unit_price": 750}


def make(number: str | None, amount: str, status: str, currency: str = "USD", **update) -> RunState:
    fields = {"invoice_number": number, "vendor_name": "Vendor [b]Co[/b]", "currency": currency, "line_items": [LINE]}
    invoice = Invoice.model_validate(dict.fromkeys(Invoice.model_fields) | fields)
    validation = ValidationReport.model_validate({"checks": [SHORT], "stock": [STOCK], "amount": amount})
    return RunState(run_id="r1", invoice_path=f"{number}-{amount}", invoice=invoice, validation=validation,
                    status=status, reasons=[SHORT["message"]], **update)


def render(fn, *args) -> str:
    console = Console(file=io.StringIO(), width=80, record=True, color_system=None)
    fn(*args, console)
    return console.export_text()


PAID = {"payments": [{"vendor": "V", "amount": "1890.00", "currency": "USD"}]}
CALLS = [ToolCall(round=n, name="payment_history", args={"invoice_number": "INV-1"}, result=r)
         for n, r in ((1, PAID), (2, {"error": "limit"}))]
VP = [Decision(action="approve", rationale="Approve [bold]now[/bold].", concerns=["[/b] pre-approved"]),
      Decision(action="approve", rationale="Still approve.", concerns=[])]
CRITIQUES = [Critique(verdict="revise", problems=["Notes say [/urgent]"]), Critique(verdict="accept", problems=[])]
TWO_ROUNDS = make("INV-1", "15000.00", "REJECTED", vp_decisions=VP, critiques=CRITIQUES, tool_calls=CALLS,
                  route="reject", overridden=True,  # the failed check overrides the VP's approval
                  extract_attempts=2, extract_problems=["total 9999 is wrong"], timings_ms={"total": 9400})


def test_card_fits_80_columns_and_shows_every_round():
    text = render(ui.render_result, TWO_ROUNDS)
    round1, round2 = text.split("VP review · round 2")
    assert "payment_history(INV-1) → paid before: $1,890.00 to V" in round1 and "Critic  ✗ revise" in round1
    assert "payment_history(INV-1) → limit" in round2 and "Critic  ✓ accepted" in round2
    for expected in ("✓ Vendor", "✗ Stock", "✗ INSUFFICIENT_STOCK  GadgetX: 20 requested, only 5 in stock",
                     "Guardrail override: the VP said approve, but checks failed.", "✘ REJECTED   Payment blocked",
                     "Extraction self-corrected on attempt 2 (attempt 1: total 9999 is wrong)", "9.40 s",
                     "Vendor [b]Co[/b]", "Approve [bold]now[/bold].", "[/b] pre-approved", "Notes say [/urgent]"):
        assert expected in text  # Grok and document text is shown as typed, never parsed as Rich markup
    assert text.count("only 5 in stock") == 1 and all(len(line) <= 80 for line in text.splitlines())
    prefetched = TWO_ROUNDS.model_copy(update={"llm_calls": [{"task": "extract", "duration_ms": 8140.0}],
                                               "timings_ms": {"total": 9400, "extract": 1.0}})
    assert "extract 8.14 s" in render(ui.render_result, prefetched)  # the Grok call's time, not the wait


def test_summary_counts_each_invoice_once_and_keeps_currencies_apart():
    states = [make("INV-1013", "22562.80", "REJECTED"), make("inv 1013", "22562.80", "REJECTED"),  # PDF and text
              make("INV-1004", "1890.00", "REJECTED"), make(None, "-250.00", "REJECTED"),
              make("INV-1012", "9975.00", "ESCALATED"), make("INV-1014", "4125", "ESCALATED", currency="EUR")]
    rows = {line.split()[0]: " ".join(line.split()) for line in render(ui.render_summary, states).splitlines()[2:]}
    assert rows["REJECTED"] == "REJECTED 4 $24,452.80 blocked before payment · 3 invoices"  # -$250 is not added
    assert rows["ESCALATED"] == "ESCALATED 2 $9,975.00 + €4,125.00 held for human review"
    console = Console(file=io.StringIO(), width=60, record=True, color_system=None)  # a narrow terminal
    ui.render_summary(states, console)
    narrow = [" ".join(line.split()) for line in console.export_text().splitlines()]
    assert "REJECTED 4 $24,452.80 blocked before" in narrow  # status, count and amount stay whole; the note wraps
    assert "ESCALATED 2 $9,975.00 + €4,125.00 held for human review" in narrow


def test_result_to_dict_and_stage_progress():
    data = ui.result_to_dict(TWO_ROUNDS.model_copy(update={"source_text": "the document"}))
    assert "source_text" not in data and json.loads(json.dumps(data)) == data and data["decision"]["concerns"] == []
    console = Console(file=io.StringIO(), width=80)  # not a terminal, like a pipe: the line is never drawn
    with ui.stage_progress(console, "invoice_1.txt", 1, 1) as on_stage:
        on_stage("load", TWO_ROUNDS)
        on_stage("critic", None)  # type: ignore[arg-type] - a broken argument is swallowed, never raised
    assert console.file.getvalue() == ""  # type: ignore[attr-defined]
