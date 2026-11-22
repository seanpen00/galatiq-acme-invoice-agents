"""Guardrail and payment agent (code only, no LLM).

``guardrail`` turns the VP's decision into a route: any failed check forces reject, so Grok can never approve a
failing invoice, and a critique still saying "revise" forces escalate. ``pay`` is the only writer of the ledger:
USD only, and never one invoice number twice, since it records a payment before calling the bank.
"""

import logging
import secrets
from decimal import Decimal
from typing import Literal

from invoice_agent import db
from invoice_agent.models import Critique, Decision, Invoice, Status, ValidationReport
from invoice_agent.validate import CENT, invoice_key, money, normalize_sku

Route = Literal["pay", "reject", "escalate"]
ROUTES: dict[str, Route] = {"approve": "pay", "reject": "reject", "escalate": "escalate"}


def mock_payment(vendor: str, amount: Decimal) -> dict:
    """The case brief's mock banking API: log the payment and report success."""
    logging.getLogger(__name__).info("Paid %s to %s", amount, vendor)
    return {"status": "success"}


def guardrail(
    validation: ValidationReport, decision: Decision, critique: Critique | None = None
) -> tuple[Route, bool, list[str]]:
    """``(route, overridden, reasons)`` for the VP's decision; ``critique`` reviews that same decision."""
    if validation.failures:
        return "reject", decision.action != "reject", [c.message for c in validation.failures]
    if critique is not None and critique.verdict == "revise":
        lead = f"Critic did not accept the VP's decision to {decision.action}"
        return "escalate", decision.action != "escalate", [f"{lead}: {p}" for p in critique.problems] or [lead]
    reasons = [decision.rationale] if decision.action == "approve" else (decision.concerns or [decision.rationale])
    return ROUTES[decision.action], False, reasons


def pay(invoice: Invoice, amount: Decimal | None, db_path: db.PathArg = None) -> tuple[Status, str | None, str]:
    """Pay an approved invoice through ``mock_payment``. Returns ``(status, payment reference, reason)``."""
    vendor, key = (invoice.vendor_name or "").strip(), invoice_key(invoice.invoice_number)
    if invoice.currency != "USD":
        return "ESCALATED", None, f"Mock payment supports USD only (invoice is {invoice.currency})"
    if amount is None or amount <= 0:
        return "REJECTED", None, f"Amount to pay must be positive (got {money(amount)})"
    if not vendor:
        return "REJECTED", None, "Vendor is missing, so there is no one to pay"
    if key is None:
        return "ESCALATED", None, "Invoice number is missing, so a duplicate can't be ruled out — needs review"
    prior = db.find_payment(key, path=db_path)
    same_vendor = prior is not None and normalize_sku(prior["vendor"]) == normalize_sku(vendor)  # case, punctuation
    if same_vendor and abs(prior["amount"] - amount) <= CENT:
        return "DUPLICATE", prior["reference"], f"Already paid on {prior['paid_at'][:10]} (ref {prior['reference']})"
    if prior is not None:  # a different amount, or another vendor reusing the number
        paid = f"already paid {money(prior['amount'])} to {prior['vendor']}"
        return "ESCALATED", None, f"{key} was {paid}; this invoice from {vendor} asks {money(amount)} — needs review"

    reference = "PAY-" + secrets.token_hex(4).upper()
    db.record_payment(key, vendor, amount, invoice.currency, reference, path=db_path)  # no ledger row, no payment
    try:
        if (reply := mock_payment(vendor, amount)).get("status") != "success":
            raise RuntimeError(f"the bank replied {reply}")
    except Exception as exc:  # noqa: BLE001 - the row stays: a re-run is a DUPLICATE, never a second payment
        failed = f"Mock payment failed for {key} ({type(exc).__name__}: {exc})"
        return "ERROR", reference, f"{failed}; ledger entry {reference} kept, reconcile it by hand"
    return "PAID", reference, f"Paid {money(amount)} to {vendor} (ref {reference})"
