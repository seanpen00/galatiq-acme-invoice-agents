"""Validation agent: SKU matching, due dates, the invoice-number key, money formatting and the checks.

Pure functions (no database, LLM or I/O), so the rules that decide whether money moves are fast to test
and cannot be talked out of. ``validate`` returns a copy of the invoice with catalog SKUs and a currency
set, and a report whose checks follow CHECK_CODES: each category gives one passed check or its failures.
"""

import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from typing import Any

from invoice_agent.models import SCRUTINY_THRESHOLD, Check, Invoice, LineItem, StockLine, ValidationReport

Catalog = Mapping[str, Mapping[str, Any]]  # {"WidgetA": {"stock": 15, "unit_price": Decimal("250.00"), ...}}

CENT = Decimal("0.01")  # money compares within one cent
CHECK_CODES = ["VENDOR", "LINE_ITEMS", "DUE_DATE", "KNOWN_ITEMS", "QUANTITIES", "STOCK", "MATH"]
FAILURE_CATEGORY = {  # every failure code -> its check category; any failure rejects the invoice
    "MISSING_VENDOR": "VENDOR", "MISSING_ITEMS": "LINE_ITEMS",
    "MISSING_DUE_DATE": "DUE_DATE", "INVALID_DUE_DATE": "DUE_DATE",
    "UNKNOWN_ITEM": "KNOWN_ITEMS", "INVALID_QUANTITY": "QUANTITIES",
    "OUT_OF_STOCK": "STOCK", "INSUFFICIENT_STOCK": "STOCK",
    # A line, the subtotal or the total more than a cent off; a negative price; no total, or a line with no amount.
    "MATH_MISMATCH": "MATH", "INVALID_PRICE": "MATH", "UNVERIFIABLE_AMOUNT": "MATH",
}
# ISO, US month-first, day-first only when month-first is impossible (30/04/2026), then month and day names:
# February 26, 2026 · Feb 26 2026 · 26 February 2026 · 26-Feb-2026. Ordinals ("20th") are dropped first.
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
                "%d %B %Y", "%d %b %Y", "%d-%b-%Y")
ORDINAL = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def money(value: Decimal | None, currency: str = "USD") -> str:
    """'$5,000.00', '-$250.00', '€4,125.00', '1,000.00 CHF'; '—' when the amount is unknown."""
    if value is None:
        return "—"
    symbol = SYMBOLS.get(currency, "")
    text = f"{'-' if value < 0 else ''}{symbol}{abs(value):,.2f}"
    return text if symbol else f"{text} {currency}"


def normalize_sku(name: str) -> str:
    """Matching key for item names, ignoring case, spaces and punctuation: 'Widget-a' -> 'widgeta'."""
    return re.sub(r"[^0-9a-z]", "", name.lower())


def invoice_key(number: str | None) -> str | None:
    """Duplicate-detection key: 'INV-' + the first run of digits ('#inv 0042-R1' -> 'INV-42'); None if none.

    Two invoices merged by mistake fail safe: the payment step calls the second a duplicate or escalates it.
    """
    match = re.search(r"\d+", number or "")
    return f"INV-{int(match.group())}" if match else None


def parse_date(text: str | None) -> date | None:
    """A date in one of DATE_FORMATS, else None (words like 'tomorrow' are not dates)."""
    cleaned = ORDINAL.sub("", (text or "").strip())
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def validate(invoice: Invoice, catalog: Catalog) -> tuple[Invoice, ValidationReport]:
    """Match each line to a catalog SKU and run every check. Returns (the invoice copy, the report)."""
    by_key = {normalize_sku(sku): sku for sku in catalog}
    lines = [line.model_copy(update={"sku": by_key.get(normalize_sku(line.item))}) for line in invoice.line_items]
    currency = (invoice.currency or "").strip().upper() or "USD"
    invoice = invoice.model_copy(update={"line_items": lines, "currency": currency})
    vendor = (invoice.vendor_name or "").strip()
    names = dict.fromkeys(line.item for line in lines if line.sku is None)  # unknown items, each once, in order
    unknown = [_fail("UNKNOWN_ITEM", f"Unknown item {name!r}: not in the inventory catalog") for name in names]
    stock_failures, stock = _stock(lines, catalog)
    results = {  # category -> (failures, message when it passes)
        "VENDOR": ([] if vendor else [_fail("MISSING_VENDOR", "Vendor name is missing")], f"Vendor: {vendor}"),
        "LINE_ITEMS": ([] if lines else [_fail("MISSING_ITEMS", "Invoice has no line items")], f"Lines: {len(lines)}"),
        "DUE_DATE": _due_date(invoice.due_date),
        "KNOWN_ITEMS": (unknown, "All items are in the inventory catalog"),
        "QUANTITIES": (_quantities(lines), "All quantities are positive numbers"),
        "STOCK": (stock_failures, "All quantities are within stock"),
        "MATH": (_math(invoice), "Arithmetic checks out"),
    }
    checks = [c for code, (fails, ok) in results.items() for c in fails or [Check(code=code, passed=True, message=ok)]]
    amount = invoice.total.quantize(CENT) if invoice.total is not None else None
    scrutiny = amount is not None and amount > SCRUTINY_THRESHOLD
    return invoice, ValidationReport(checks=checks, stock=stock, amount=amount, requires_scrutiny=scrutiny)


def _fail(code: str, message: str) -> Check:
    return Check(code=code, passed=False, message=message)


def _rate(text: str | None) -> Decimal | None:
    """A printed tax rate as a fraction: '8%' or '0.08' -> 0.08 (a bare number above 1 is a percent)."""
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    if match is None:
        return None
    value = Decimal(match.group())
    return value / 100 if "%" in (text or "") or value > 1 else value


def _qty(value: Decimal) -> str:
    return f"{value.normalize():f}"  # 20, 2.5, -5


def _due_date(text: str | None) -> tuple[list[Check], str]:
    text = (text or "").strip()
    if (due := parse_date(text)) is not None:
        return [], f"Due {due.isoformat()}"
    if text:
        return [_fail("INVALID_DUE_DATE", f"Due date {text!r} is not a valid date")], ""
    return [_fail("MISSING_DUE_DATE", "Due date is missing")], ""


def _quantities(lines: list[LineItem]) -> list[Check]:
    failures = []
    for i, line in enumerate(lines, start=1):
        if line.quantity is None or line.quantity <= 0:
            reason = "is missing" if line.quantity is None else f"{_qty(line.quantity)} must be positive"
            failures.append(_fail("INVALID_QUANTITY", f"Line {i} ({line.item}): quantity {reason}"))
    return failures


def _stock(lines: list[LineItem], catalog: Catalog) -> tuple[list[Check], list[StockLine]]:
    """Sum the positive quantities per SKU across lines and compare each sum with the catalog's stock."""
    requested: dict[str, list[Decimal]] = {}  # sku -> the quantity of each line with that sku
    for line in lines:
        if line.sku is not None:  # unknown items fail KNOWN_ITEMS instead
            quantities = requested.setdefault(line.sku, [])
            if line.quantity is not None and line.quantity > 0:  # invalid quantities fail QUANTITIES instead
                quantities.append(line.quantity)
    failures, stock = [], []
    for sku, quantities in requested.items():
        qty, in_stock = sum(quantities, Decimal(0)), catalog[sku]["stock"]
        stock.append(StockLine(sku=sku, requested=qty, in_stock=in_stock, unit_price=catalog[sku].get("unit_price")))
        if in_stock <= 0:
            failures.append(_fail("OUT_OF_STOCK", f"{sku}: {_qty(qty)} requested, but it is out of stock"))
        elif qty > in_stock:
            across = f" across {len(quantities)} lines" if len(quantities) > 1 else ""
            message = f"{sku}: {_qty(qty)} requested{across}, only {in_stock} in stock"
            failures.append(_fail("INSUFFICIENT_STOCK", message))
    return failures, stock


def _math(invoice: Invoice) -> list[Check]:
    """Recompute each line, the subtotal and the total; flag negative prices and amounts that cannot be checked."""
    fmt = partial(money, currency=invoice.currency or "USD")
    failures, amounts = [], []  # amounts: each line's amount as printed, else quantity x unit price
    for i, line in enumerate(invoice.line_items, start=1):
        qty, price, stated, label = line.quantity, line.unit_price, line.line_total, f"Line {i} ({line.item})"
        extended = qty * price if qty is not None and price is not None else None
        if extended is not None and stated is not None and abs(stated - extended) > CENT:
            shows = f"{_qty(qty)} x {fmt(price)} = {fmt(extended)}, but the line shows {fmt(stated)}"
            failures.append(_fail("MATH_MISMATCH", f"{label}: {shows}"))
        for name, value in (("unit price", price), ("line amount", stated)):
            if value is not None and value < 0:
                failures.append(_fail("INVALID_PRICE", f"{label}: {name} {fmt(value)} must not be negative"))
        amounts.append(stated if stated is not None else extended)
        if amounts[-1] is None:
            failures.append(_fail("UNVERIFIABLE_AMOUNT", f"{label} has no amount, so the total cannot be verified"))
    if invoice.total is None:
        failures.append(_fail("UNVERIFIABLE_AMOUNT", "No total is stated, so the amount due cannot be confirmed"))
    if not amounts or any(amount is None for amount in amounts):
        return failures  # nothing to add up, or a line already failed as unverifiable
    lines_sum = sum(amounts, Decimal(0))
    if invoice.subtotal is not None and abs(invoice.subtotal - lines_sum) > CENT:
        message = f"Subtotal {fmt(invoice.subtotal)} does not match the line items, which add up to {fmt(lines_sum)}"
        failures.append(_fail("MATH_MISMATCH", message))
    base = invoice.subtotal if invoice.subtotal is not None else lines_sum
    rate = _rate(invoice.tax_rate)
    if rate is not None and invoice.tax_amount is not None and abs(base * rate - invoice.tax_amount) > CENT:
        expected = f"{invoice.tax_rate} of {fmt(base)} is {fmt((base * rate).quantize(CENT))}"
        failures.append(_fail("MATH_MISMATCH", f"Tax {fmt(invoice.tax_amount)} does not match the rate: {expected}"))
    extras = [(v, name) for v, name in ((invoice.tax_amount, "tax"), (invoice.shipping, "shipping")) if v]
    computed = base + sum((v for v, _ in extras), Decimal(0))
    if invoice.total is not None and abs(invoice.total - computed) > CENT:
        parts = [f"{fmt(base)} subtotal"] + [f"{fmt(v)} {name}" for v, name in extras]
        breakdown = f"{' + '.join(parts)} = {fmt(computed)}" if extras else f"the {parts[0]}"
        gap = f"{fmt(abs(invoice.total - computed))} {'over' if invoice.total > computed else 'short'}"
        failures.append(_fail("MATH_MISMATCH", f"Total {fmt(invoice.total)} does not match {breakdown} ({gap})"))
    return failures
