"""Ingestion agent: an invoice file becomes text (pdfplumber for PDFs, no OCR); Grok copies it into an Invoice.

``ungrounded`` lists the numbers Grok returned that the document does not print, for ``extract`` to retry with.
"""

import re
import threading
from decimal import Decimal
from pathlib import Path

import pdfplumber

from invoice_agent import llm
from invoice_agent.models import Invoice

SUPPORTED_EXTENSIONS = frozenset({".txt", ".json", ".csv", ".xml", ".pdf"})
PROMPT_PATH = Path(__file__).parent / "prompts" / "extract.md"
PDF_LOCK = threading.Lock()  # pdfminer is not documented as thread-safe, and main.py reads files in a pool
NUMBER = re.compile(r"\d[\dOo,]*(?:\.[\dOo]+)?")  # a printed number; a letter O in it is a misprinted 0
EUROPEAN = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}\b")  # 1.000,00 or 250,00: dots group thousands, comma decimals
RETRY = ("\n\n<previous_attempt_problems>\nYour previous answer for this document was rejected by Acme's checks:\n{}\n"
         "Copy every value from the document; never compute, round or correct one.\n</previous_attempt_problems>")


class IngestError(Exception):
    """The file cannot be turned into text; the message is safe to show to a user."""


def read_text(path: Path) -> str:
    """The text of an invoice file. Raises IngestError for a missing, unsupported, unreadable or empty file."""
    suffix = path.suffix.lower()
    if not path.exists():
        raise IngestError(f"File not found: {path}")
    if suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise IngestError(f"Unsupported file type '{suffix or '(none)'}' for {path.name}. Supported: {supported}.")
    try:
        if suffix == ".pdf":
            with PDF_LOCK, pdfplumber.open(path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        else:
            text = path.read_text(encoding="utf-8-sig", errors="replace")  # utf-8-sig drops a byte-order mark
    except Exception as exc:
        raise IngestError(f"Could not read {path.name}: {exc}") from exc
    if not text.strip():
        hint = " (scanned PDFs without a text layer are not supported)" if suffix == ".pdf" else ""
        raise IngestError(f"{path.name} contains no readable text{hint}.")
    return text


def extract(text: str, filename: str, problems: list[str] | None = None) -> tuple[Invoice, dict]:
    """Ask Grok to copy the invoice out of ``text``; ``problems`` say why the previous attempt was rejected."""
    user = f"File name: {filename}\n\n<document>\n{text}\n</document>"
    if problems:
        user += RETRY.format("\n".join(f"- {p}" for p in problems))
    return llm.invoke_structured("extract", Invoice, PROMPT_PATH.read_text(encoding="utf-8"), user)


def ungrounded(invoice: Invoice, text: str) -> list[str]:
    """The numbers in ``invoice`` that ``text`` does not print, compared as absolute Decimals (250 = $250.00)."""
    printed = set()
    for token in NUMBER.findall(text):
        token = re.sub("[Oo]", "0", token)
        pieces = [token.replace(",", ""), *token.split(",")]  # each comma piece counts too, for CSV rows
        printed.update(abs(Decimal(piece)) for piece in pieces if piece)
    printed.update(Decimal(n.replace(".", "").replace(",", ".")) for n in EUROPEAN.findall(text))
    values = [(f"line {i} ({line.item}) {name}", getattr(line, name)) for i, line in enumerate(invoice.line_items, 1)
              for name in ("quantity", "unit_price", "line_total")]
    values += [(name, getattr(invoice, name)) for name in ("subtotal", "tax_amount", "shipping", "total")]
    return [f"{label} {value} does not appear in the document" for label, value in values
            if value is not None and abs(value) not in printed]
