"""The guardrail matrix and the payment agent's ledger rules (offline, on a temporary SQLite DB)."""

import sqlite3
from contextlib import closing
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from invoice_agent import payment
from invoice_agent.models import Check, Critique, Decision, Invoice, ValidationReport

ACCEPT = Critique(verdict="accept", problems=[])
REVISE = Critique(verdict="revise", problems=["The address is a landmark", "Price is 32% over catalog"])


def report(*failed: str) -> ValidationReport:
    failures = [Check(code=code, passed=False, message=f"{code} detail") for code in failed]
    return ValidationReport(checks=[Check(code="VENDOR", passed=True, message="Vendor: Widgets Inc."), *failures])


def decision(action: str, concerns: list[str] | None = None) -> Decision:
    return Decision(action=action, rationale=f"The VP chose to {action}.", concerns=concerns or [])


def invoice(number: str | None = "INV-1001", currency: str = "USD", vendor: str | None = "Widgets Inc.") -> Invoice:
    fields = dict.fromkeys(Invoice.model_fields) | {"line_items": []}
    return Invoice.model_validate(fields | {"invoice_number": number, "currency": currency, "vendor_name": vendor})


@pytest.fixture
def bank(monkeypatch) -> list[tuple]:
    """Every call that reached the mock bank."""
    calls: list[tuple] = []
    monkeypatch.setattr(payment, "mock_payment", lambda *args: calls.append(args) or {"status": "success"})
    return calls


def ledger(db_path) -> list[tuple]:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT invoice_number, vendor, amount, currency FROM payments").fetchall()


@pytest.mark.parametrize("critique", [None, ACCEPT, REVISE])
@pytest.mark.parametrize("action", ["approve", "reject", "escalate"])
def test_any_failed_check_rejects_whatever_the_vp_or_critic_says(action, critique):
    result = payment.guardrail(report("OUT_OF_STOCK", "MISSING_DUE_DATE"), decision(action), critique)
    assert result == ("reject", action != "reject", ["OUT_OF_STOCK detail", "MISSING_DUE_DATE detail"])


@pytest.mark.parametrize(("action", "route", "reasons"), [
    ("approve", "pay", ["The VP chose to approve."]),  # an approval's reason is its rationale, even with concerns
    ("reject", "reject", ["Fake address"]),
    ("escalate", "escalate", ["Fake address"]),
])
def test_a_clean_invoice_follows_the_vp_unless_the_critic_said_revise(action, route, reasons):
    for critique in (None, ACCEPT):
        assert payment.guardrail(report(), decision(action, ["Fake address"]), critique) == (route, False, reasons)
    lead = f"Critic did not accept the VP's decision to {action}"
    assert payment.guardrail(report(), decision(action), REVISE) == (
        "escalate", action != "escalate", [f"{lead}: {p}" for p in REVISE.problems]
    )


def test_a_clean_usd_invoice_is_paid_once_and_recorded(db_path, bank):
    status, ref, reason = payment.pay(invoice(), Decimal("5000.00"), db_path)
    assert (status, reason) == ("PAID", f"Paid $5,000.00 to Widgets Inc. (ref {ref})") and ref.startswith("PAY-")
    assert bank == [("Widgets Inc.", Decimal("5000.00"))]
    assert ledger(db_path) == [("INV-1001", "Widgets Inc.", "5000.00", "USD")]

    for amount in ("5000.00", "5000.01", "4999.99"):  # within a cent: the same payment again
        again = payment.pay(invoice("inv 1001 rev 2"), Decimal(amount), db_path)  # same key: INV-1001
        assert again[:2] == ("DUPLICATE", ref) and again[2].startswith("Already paid on 20")
    assert len(bank) == 1 and len(ledger(db_path)) == 1


def test_a_different_amount_or_vendor_under_a_paid_number_is_escalated(db_path, bank):
    payment.pay(invoice("INV-1004"), Decimal("1890.00"), db_path)
    assert payment.pay(invoice("INV-1004"), Decimal("5940.00"), db_path) == ("ESCALATED", None, "INV-1004 was already "
        "paid $1,890.00 to Widgets Inc.; this invoice from Widgets Inc. asks $5,940.00 — needs review")
    other = payment.pay(invoice("INV-1004", vendor="Other LLC"), Decimal("1890.00"), db_path)  # same amount
    assert other[:2] == ("ESCALATED", None) and "this invoice from Other LLC asks $1,890.00" in other[2]
    assert len(bank) == 1 and len(ledger(db_path)) == 1


@pytest.mark.parametrize(("inv", "amount", "status", "reason"), [
    (invoice(currency="EUR"), Decimal("4125.00"), "ESCALATED", "Mock payment supports USD only (invoice is EUR)"),
    (invoice(), Decimal("0"), "REJECTED", "Amount to pay must be positive (got $0.00)"),
    (invoice(), Decimal("-250.00"), "REJECTED", "Amount to pay must be positive (got -$250.00)"),
    (invoice(), None, "REJECTED", "Amount to pay must be positive (got —)"),
    (invoice(vendor=" "), Decimal("10"), "REJECTED", "Vendor is missing, so there is no one to pay"),
    (invoice(number="N/A"), Decimal("10"), "ESCALATED",
     "Invoice number is missing, so a duplicate can't be ruled out — needs review"),
])
def test_an_unpayable_invoice_sends_no_money(db_path, bank, inv, amount, status, reason):
    assert payment.pay(inv, amount, db_path) == (status, None, reason)
    assert bank == [] and ledger(db_path) == []


@pytest.mark.parametrize(("bank_call", "why"), [
    (MagicMock(side_effect=RuntimeError("bank offline")), "bank offline"),
    (MagicMock(return_value={"status": "declined"}), "the bank replied {'status': 'declined'}"),
])
def test_a_failed_or_declined_bank_call_is_an_error_and_never_paid_twice(db_path, monkeypatch, bank_call, why):
    monkeypatch.setattr(payment, "mock_payment", bank_call)
    status, ref, reason = payment.pay(invoice(), Decimal("5000.00"), db_path)
    assert (status, reason) == ("ERROR", f"Mock payment failed for INV-1001 (RuntimeError: {why}); "
                                         f"ledger entry {ref} kept, reconcile it by hand")
    assert payment.pay(invoice(), Decimal("5000.00"), db_path)[:2] == ("DUPLICATE", ref)  # the row stayed
    assert len(ledger(db_path)) == 1
