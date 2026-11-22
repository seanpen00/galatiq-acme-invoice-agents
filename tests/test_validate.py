"""Tests for the typed core: validate (checks, SKUs, keys, money), ingest (read_text, extract, grounding), the schema.

The 22 hand-checked extractions in tests/fixtures/extractions are what a perfect extractor returns for
data/invoices and fixtures/invoices. Against the seed catalog they must reproduce the code-only outcomes
in ORACLE exactly. No LLM and no database are needed.
"""

import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_agent import ingest, llm
from invoice_agent.db import SEED_INVENTORY
from invoice_agent.models import Invoice, LineItem
from invoice_agent.validate import CHECK_CODES, FAILURE_CATEGORY, invoice_key, money, normalize_sku, parse_date
from invoice_agent.validate import validate

ROOT = Path(__file__).resolve().parents[1]
EXTRACTIONS = Path(__file__).parent / "fixtures" / "extractions"
SOURCES = {p.name: p for d in ("data/invoices", "fixtures/invoices") for p in sorted((ROOT / d).iterdir())}
CATALOG = {item: {"stock": n, "unit_price": p and Decimal(p)} for item, n, p in SEED_INVENTORY}
A, B, X, F = "WidgetA", "WidgetB", "GadgetX", "FakeItem"
STOCK, MATH = "INSUFFICIENT_STOCK", "MATH_MISMATCH"

# file -> (failure codes, amount, requires_scrutiny, currency, skus) that code alone decides for each file.
ORACLE = {
    "invoice_1001.txt": ([], "5000.00", False, "USD", [A, B]),
    "invoice_1002.txt": ([STOCK], "15000.00", True, "USD", [X]),
    "invoice_1003.txt": (["INVALID_DUE_DATE", "OUT_OF_STOCK"], "100000.00", True, "USD", [F]),
    "invoice_1004.json": ([], "1890.00", False, "USD", [A, B]),
    "invoice_1004_revised.json": ([], "5940.00", False, "USD", [A, B, X]),
    "invoice_1005.json": ([STOCK], "15225.00", True, "USD", [A, X, B]),
    "invoice_1006.csv": ([], "2750.00", False, "USD", [A, B]),
    "invoice_1007.csv": ([STOCK, STOCK, MATH], "15525.00", True, "USD", [A, B, X]),
    "invoice_1008.txt": (["UNKNOWN_ITEM"] * 2, "9900.00", False, "USD", [None, None]),
    "invoice_1009.json": (["MISSING_VENDOR", "MISSING_DUE_DATE", "INVALID_QUANTITY", MATH, MATH], "-250.00", False,
                          "USD", [A, B]),
    "invoice_1010.txt": ([], "7185.00", False, "USD", [A, B, X, A]),
    "invoice_1011.pdf": ([], "3000.00", False, "USD", [A, B]),
    "invoice_1011.txt": ([], "3000.00", False, "USD", [A, B]),
    "invoice_1012.pdf": ([], "9975.00", False, "USD", [A, B, X]),
    "invoice_1012.txt": ([], "9975.00", False, "USD", [A, B, X]),
    "invoice_1013.json": ([STOCK] * 3 + [MATH], "22562.80", True, "USD", [A, B, X, A, B, X, A, X]),
    "invoice_1013.pdf": ([STOCK] * 3 + [MATH], "22562.80", True, "USD", [A, B, X, A, B, X, A, X]),
    "invoice_1014.xml": ([], "4125.00", False, "EUR", [A, B]),
    "invoice_1015.csv": ([], "6500.00", False, "USD", [A, B, X]),
    "invoice_1016.json": (["UNKNOWN_ITEM"], "3233.00", False, "USD", [A, B, None]),
    "invoice_2001.txt": ([], "12500.00", True, "USD", [A, B, X]),
    "invoice_2002.txt": ([STOCK], "7000.00", False, "USD", [A, X]),
}

# The brief's traps and the extra ones: every failure message, in report order.
MESSAGES = {
    "invoice_1002.txt": ["GadgetX: 20 requested, only 5 in stock"],
    "invoice_1003.txt": ["Due date 'yesterday' is not a valid date", "FakeItem: 100 requested, but it is out of stock"],
    "invoice_1007.csv": [
        "WidgetA: 20 requested, only 15 in stock",
        "WidgetB: 15 requested, only 10 in stock",
        "Total $15,525.00 does not match $14,750.00 subtotal + $885.00 tax = $15,635.00 ($110.00 short)",
    ],
    "invoice_1008.txt": [
        "Unknown item 'SuperGizmo': not in the inventory catalog",
        "Unknown item 'MegaSprocket': not in the inventory catalog",
    ],
    "invoice_1009.json": [
        "Vendor name is missing",
        "Due date is missing",
        "Line 1 (WidgetA): quantity -5 must be positive",
        "Subtotal $1,000.00 does not match the line items, which add up to -$250.00",
        "Total -$250.00 does not match the $1,000.00 subtotal ($1,250.00 short)",
    ],
    "invoice_1013.json": [
        "WidgetA: 22 requested across 3 lines, only 15 in stock",
        "WidgetB: 18 requested across 2 lines, only 10 in stock",
        "GadgetX: 9 requested across 3 lines, only 5 in stock",
        "Total $22,562.80 does not match $21,040.00 subtotal + $1,472.80 tax = $22,512.80 ($50.00 over)",
    ],
    "invoice_1016.json": ["Unknown item 'WidgetC': not in the inventory catalog"],
    "invoice_2002.txt": ["GadgetX: 8 requested, only 5 in stock"],
}


def fixture(name: str) -> Invoice:
    return Invoice.model_validate_json((EXTRACTIONS / f"{name}.json").read_text(encoding="utf-8"))


def line(item: str, qty: str | None = "1", price: str | None = None, total: str | None = None) -> LineItem:
    return LineItem(item=item, quantity=qty, unit_price=price, line_total=total, note=None)


def invoice(*lines: LineItem, **fields: object) -> Invoice:
    """A clean invoice (vendor and due date set) around ``lines``; every other field null unless given."""
    data = dict.fromkeys(Invoice.model_fields) | {"vendor_name": "Test Vendor", "due_date": "2026-03-01"}
    return Invoice.model_validate(data | {"line_items": list(lines)} | fields)


def failures(inv: Invoice) -> list[str]:
    return [c.message for c in validate(inv, CATALOG)[1].failures]


def test_one_extraction_per_source_file() -> None:
    assert sorted(p.name for p in EXTRACTIONS.glob("*.json")) == sorted(f"{name}.json" for name in ORACLE)
    assert sorted(SOURCES) == sorted(ORACLE)


@pytest.mark.parametrize("name", ORACLE)
def test_extraction_reproduces_the_oracle(name: str) -> None:
    codes, amount, scrutiny, currency, skus = ORACLE[name]
    text = ingest.read_text(SOURCES[name])
    assert ingest.ungrounded(fixture(name), text) == []
    checked, report = validate(fixture(name), CATALOG)
    assert Counter(c.code for c in report.failures) == Counter(codes)
    assert (str(report.amount), report.requires_scrutiny, checked.currency) == (amount, scrutiny, currency)
    assert invoice_key(checked.invoice_number) == f"INV-{name[8:12]}"
    assert [ln.sku for ln in checked.line_items] == skus
    # Every category in CHECK_CODES order: one passed check, or only its failures.
    categories = [c.code if c.passed else FAILURE_CATEGORY[c.code] for c in report.checks]
    assert list(dict.fromkeys(categories)) == CHECK_CODES
    # Text fields are verbatim (whitespace collapsed; notes join separate pieces with a newline).
    data, squashed = json.loads((EXTRACTIONS / f"{name}.json").read_text()), " ".join(text.split())
    values = [v for k, v in data.items() if isinstance(v, str) and k != "currency"]
    values += [v for ln in data["line_items"] for v in ln.values() if isinstance(v, str)]
    assert [v for v in values for piece in v.split("\n") if " ".join(piece.split()) not in squashed] == []


@pytest.mark.parametrize("name", MESSAGES)
def test_scenario_messages(name: str) -> None:
    assert failures(fixture(name)) == MESSAGES[name]


def test_stock_is_summed_per_sku_and_skips_unknown_items_and_invalid_quantities() -> None:
    requested = {n: {s.sku: s.requested for s in validate(fixture(n), CATALOG)[1].stock} for n in MESSAGES}
    assert requested["invoice_1013.json"] == {A: 22, B: 18, X: 9}
    assert requested["invoice_1008.txt"] == {}
    assert requested["invoice_1009.json"] == {A: 0, B: 2}  # the -5 is not counted
    assert requested["invoice_1016.json"] == {A: 4, B: 2}  # WidgetC stays unknown
    stock = validate(fixture("invoice_1002.txt"), CATALOG)[1].stock
    assert [(s.sku, s.in_stock, s.unit_price) for s in stock] == [(X, 5, Decimal("750.00"))]


def test_validate_returns_a_copy() -> None:
    original = fixture("invoice_1012.txt")
    checked, _ = validate(original, CATALOG)
    assert [ln.sku for ln in checked.line_items] == [A, B, X]  # 'Widget A' and 'Gadget X' match by normalize_sku
    assert [ln.sku for ln in original.line_items] == [None, None, None] and original.currency is None
    assert checked.line_items[1].line_total == Decimal("3500.00")  # the misprinted '$3,500.O0', read as a number
    currencies = [validate(invoice(currency=c), CATALOG)[0].currency for c in (" usd ", "eur", "", None)]
    assert currencies == ["USD", "EUR", "USD", "USD"]  # stripped and upper-cased; none stated means USD


@pytest.mark.parametrize(("qty", "expected"), [("5", []), ("6", ["GadgetX: 6 requested, only 5 in stock"])])
def test_quantity_equal_to_stock_passes(qty: str, expected: list[str]) -> None:
    assert failures(invoice(line(X, qty, "750.00"), total=Decimal(750) * int(qty))) == expected


@pytest.mark.parametrize(("total", "scrutiny"), [("10000.00", False), ("10000.01", True)])
def test_scrutiny_is_strictly_above_10000(total: str, scrutiny: bool) -> None:
    _, report = validate(invoice(line(A, "10", "1000.00"), shipping=Decimal(total) - 10000, total=total), CATALOG)
    assert report.failures == [] and report.requires_scrutiny is scrutiny
    assert report.amount == Decimal(total)


@pytest.mark.parametrize(("lines", "totals", "expected"), [
    ([line(A, "2", "250.00")], {"total": "500.01"}, []),  # within one cent
    ([line(A, "2", "250.00")], {"total": "500.02"}, ["Total $500.02 does not match the $500.00 subtotal ($0.02 over)"]),
    ([line(A, "2", "250.00")], {"total": "499.98"},
     ["Total $499.98 does not match the $500.00 subtotal ($0.02 short)"]),
    ([line(A, "2", "250.00", "600.00")], {"total": "600.00"},
     ["Line 1 (WidgetA): 2 x $250.00 = $500.00, but the line shows $600.00"]),
    ([line(A, "10", None, "2500.00")], {"total": "2500.00"}, []),  # a printed line amount alone is enough
    ([line(A, "2", "250.00", "500.00"), line(B, "1", "-100.00", "-100.00")], {"total": "400.00"},  # adds up, but
     ["Line 2 (WidgetB): unit price -$100.00 must not be negative",  # a negative price is invalid like a negative qty
      "Line 2 (WidgetB): line amount -$100.00 must not be negative"]),
    ([line(A, "2", "250.00")], {}, ["No total is stated, so the amount due cannot be confirmed"]),
    ([line(A, "2", "250.00"), line(B, "5")], {"total": "9000.00"},  # an unpriced line: never pay just the priced part
     ["Line 2 (WidgetB) has no amount, so the total cannot be verified"]),
    ([line(A, None, "250.00", "250.00"), line(B, "0", "500.00")], {"total": "250.00"},
     ["Line 1 (WidgetA): quantity is missing", "Line 2 (WidgetB): quantity 0 must be positive"]),
    ([], {"total": "100.00"}, ["Invoice has no line items"]),
    ([line("WidgetC", "1", "10.00"), line("WidgetC", "2", "10.00")], {"total": "30.00"},
     ["Unknown item 'WidgetC': not in the inventory catalog"]),  # reported once
])
def test_rules(lines: list[LineItem], totals: dict[str, str], expected: list[str]) -> None:
    assert failures(invoice(*lines, **totals)) == expected


def test_shipping_and_tax_count_toward_the_total() -> None:
    lines = [line(A, "2", "250.00"), line(B, "1", "500.00")]
    assert failures(invoice(*lines, subtotal="1000", tax_amount="50", shipping="10", total="1060.00")) == []
    assert failures(invoice(*lines, tax_amount="50", shipping="10", total="1000.00")) == [
        "Total $1,000.00 does not match $1,000.00 subtotal + $50.00 tax + $10.00 shipping = $1,060.00 ($60.00 short)"
    ]


@pytest.mark.parametrize("rate", ["5%", "0.05", "5"])
def test_tax_must_match_its_printed_rate_even_when_the_total_adds_up(rate: str) -> None:
    lines = [line(A, "2", "250.00"), line(B, "1", "500.00")]
    assert failures(invoice(*lines, subtotal="1000", tax_rate=rate, tax_amount="50", total="1050")) == []
    assert failures(invoice(*lines, subtotal="1000", tax_rate=rate, tax_amount="400", total="1400")) == [
        f"Tax $400.00 does not match the rate: {rate} of $1,000.00 is $50.00"
    ]


def test_every_failure_code_maps_to_a_check_category() -> None:
    assert set(FAILURE_CATEGORY.values()) == set(CHECK_CODES)


@pytest.mark.parametrize(("number", "key"), [
    ("INV-1001", "INV-1001"), ("INV 1012", "INV-1012"), ("#INV-01004-R1", "INV-1004"), ("1002", "INV-1002"),
    ("PO-ABC", None), (None, None),
])
def test_invoice_key(number: str | None, key: str | None) -> None:
    assert invoice_key(number) == key


def test_sku_matching_ignores_case_spaces_and_punctuation_only() -> None:
    assert normalize_sku("Widget A") == normalize_sku(" widget-a ") == normalize_sku("WidgetA") == "widgeta"
    assert normalize_sku("WidgetC") != normalize_sku("WidgetA")


@pytest.mark.parametrize(("text", "parsed"), [
    ("2026-02-01", "2026-02-01"), ("02/28/2026", "2026-02-28"), ("February 26, 2026", "2026-02-26"),
    ("Jan 30 2026", "2026-01-30"), ("Jan 30, 2026", "2026-01-30"), ("25-Feb-2026", "2026-02-25"),
    ("15 April 2026", "2026-04-15"), ("April 20th, 2026", "2026-04-20"), ("1st Mar 2026", "2026-03-01"),
    ("30/04/2026", "2026-04-30"), ("04/05/2026", "2026-04-05"),  # day-first only when month-first is impossible
    ("yesterday", None), ("", None), (None, None),
])
def test_parse_date(text: str | None, parsed: str | None) -> None:
    due = parse_date(text)
    assert (due.isoformat() if due else None) == parsed


def test_money() -> None:
    assert [money(Decimal("5000")), money(Decimal("-250.5")), money(Decimal("4125"), "EUR")] == [
        "$5,000.00", "-$250.50", "€4,125.00"
    ]
    assert (money(Decimal("1000"), "CHF"), money(None)) == ("1,000.00 CHF", "—")


def test_schema_is_valid_for_strict_mode() -> None:
    schema = Invoice.model_json_schema()
    assert '"pattern":' not in json.dumps(schema) and '"default":' not in json.dumps(schema)
    numbers = {"quantity", "unit_price", "line_total", "subtotal", "tax_amount", "shipping", "total"}
    for obj in (schema, schema["$defs"]["LineItem"]):
        assert obj["required"] == list(obj["properties"])  # strict mode: every field required (nullable)
        for name, prop in obj["properties"].items():
            types = [t.get("type") for t in prop.get("anyOf", [prop])]
            assert types in ([["number", "null"]] if name in numbers else [["string"], ["string", "null"], ["array"]])
    assert "sku" not in schema["$defs"]["LineItem"]["properties"]  # code-only


def test_json_numbers_become_exact_decimals() -> None:
    data = json.loads('{"quantity": 3, "unit_price": 0.1, "line_total": 22562.80, "item": "X", "note": null}')
    parsed = LineItem.model_validate(data)
    assert (parsed.quantity, parsed.unit_price, parsed.line_total) == (3, Decimal("0.1"), Decimal("22562.80"))
    assert str(parsed.unit_price) == "0.1"  # via str(float), not the binary 0.1000000000000000055...


def _sample_phrases() -> set[str]:
    """Every 3-word run printed in a sample, and every text value of the hand-checked extractions."""
    phrases: set[str] = set()
    for name, path in SOURCES.items():
        words = ingest.read_text(path).lower().split()
        phrases |= {" ".join(words[i : i + 3]) for i in range(len(words) - 2)}
        data = json.loads((EXTRACTIONS / f"{name}.json").read_text())
        values = [v for k, v in data.items() if isinstance(v, str) and k != "currency"]  # codes like USD are generic
        phrases |= {p.lower() for v in values + [ln["item"] for ln in data["line_items"]] for p in v.split("\n")}
    return phrases


def _leaks(text: str, phrases: set[str]) -> list[str]:
    text = " ".join(text.lower().split())
    return sorted(p for p in phrases if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", text))


def test_prompt_and_schema_examples_are_not_taken_from_the_samples() -> None:
    phrases = _sample_phrases()
    described = [ingest.PROMPT_PATH.read_text(encoding="utf-8"), Invoice.__doc__, LineItem.__doc__]
    described += [f.description or "" for model in (Invoice, LineItem) for f in model.model_fields.values()]
    assert _leaks("\n".join(described), phrases) == []
    assert {"bill to: acme", "yesterday"} <= set(_leaks("a due date of Yesterday; Bill To: Acme Corp", phrases))


def test_extract_prompt_names_every_schema_field() -> None:
    prompt = ingest.PROMPT_PATH.read_text(encoding="utf-8")
    fields = (set(Invoice.model_fields) | set(LineItem.model_fields)) - {"sku"}
    assert sorted(f for f in fields if f"`{f}`" not in prompt) == []


@pytest.mark.parametrize(("name", "content", "error"), [
    ("missing.txt", None, "File not found"),
    ("invoice.docx", b"Vendor: V", r"Unsupported file type '\.docx'.*\.pdf"),
    ("invoice", b"Vendor: V", r"Unsupported file type '\(none\)'"),
    ("empty.csv", b"  \n\t\n", "empty.csv contains no readable text"),
    ("broken.pdf", b"this is not a pdf", "Could not read broken.pdf"),
    ("scan.pdf",  # one page with no text layer: what a scanned invoice looks like to pdfplumber
     b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> "
     b"endobj\n3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\n"
     b"trailer << /Root 1 0 R >>\n%%EOF\n",
     "no readable text.*scanned PDFs"),
])
def test_read_text_errors(tmp_path: Path, name: str, content: bytes | None, error: str) -> None:
    if content is not None:
        (tmp_path / name).write_bytes(content)
    with pytest.raises(ingest.IngestError, match=error):
        ingest.read_text(tmp_path / name)


def test_read_text_returns_text_unchanged_without_a_bom(tmp_path: Path) -> None:
    path = tmp_path / "INVOICE.TXT"
    path.write_bytes(b"\xef\xbb\xbfVendor: Caf\xe9\n")
    assert ingest.read_text(path) == "Vendor: Caf�\n"  # BOM dropped, invalid UTF-8 replaced
    assert ingest.read_text(SOURCES["invoice_1004.json"]) == SOURCES["invoice_1004.json"].read_text(encoding="utf-8")


def test_extract_sends_the_document_and_appends_retry_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(llm, "invoke_structured", lambda *args: calls.append(args) or ("invoice", {"task": "extract"}))
    document = "File name: a.txt\n\n<document>\nTotal: $5\n</document>"
    assert ingest.extract("Total: $5", "a.txt") == ("invoice", {"task": "extract"})
    ingest.extract("Total: $5", "a.txt", [])
    ingest.extract("Total: $5", "a.txt", ["total 6 does not appear in the document", "line 1 (X) quantity 2 ..."])
    assert [c[:3] for c in calls] == [("extract", Invoice, ingest.PROMPT_PATH.read_text(encoding="utf-8"))] * 3
    assert [c[3] for c in calls[:2]] == [document, document]
    assert calls[2][3] == document + (
        "\n\n<previous_attempt_problems>\n"
        "Your previous answer for this document was rejected by Acme's checks:\n"
        "- total 6 does not appear in the document\n"
        "- line 1 (X) quantity 2 ...\n"
        "Copy every value from the document; never compute, round or correct one.\n"
        "</previous_attempt_problems>"
    )


def test_grounding_catches_computed_and_changed_values() -> None:
    altered = fixture("invoice_1001.txt")
    altered.line_items[0].line_total = Decimal("2500.00")  # computed: 10 x $250.00 is printed nowhere
    altered.line_items[1].unit_price = Decimal("400.00")
    altered.total = Decimal("9999")
    assert ingest.ungrounded(altered, ingest.read_text(SOURCES["invoice_1001.txt"])) == [
        "line 1 (WidgetA) line_total 2500.00 does not appear in the document",
        "line 2 (WidgetB) unit_price 400.00 does not appear in the document",
        "total 9999 does not appear in the document",
    ]


def test_grounding_reads_letter_o_csv_pieces_and_signs() -> None:
    lines = [line(A, "-12", "3500", "-42000.00"), line(B, "20", "250.50", "5010")]
    grounded = invoice(*lines, total="5010.00")
    assert ingest.ungrounded(grounded, "WidgetA x-12 $3,5OO.0O ($42,OOO.00)\nINV-7,WidgetB,20,250.50,5010.00") == []
    european = invoice(line(A, "4", "250", "1000"), total="1000")
    assert ingest.ungrounded(european, "WidgetA 4 250,00 EUR 1.000,00 EUR\nGesamtbetrag: 1.000,00 EUR") == []
