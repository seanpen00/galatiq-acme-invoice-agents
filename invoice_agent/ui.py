"""Terminal output: the intro, the progress line, one card per invoice, the folder summary and the --json form.

Rendering never changes the state and copes with partial states (an ERROR before anything was extracted).
Document and Grok text is always wrapped in ``Text``, so a '[bold]' in it is shown, never parsed as markup.
"""

import textwrap
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from invoice_agent.graph import StageFn
from invoice_agent.models import Invoice, RunState, ToolCall, ValidationReport
from invoice_agent.validate import CHECK_CODES, FAILURE_CATEGORY, invoice_key, money, parse_date

STATUS = {  # status -> (color, icon, verdict headline, summary note)
    "PAID": ("green", "✔", "Payment sent", "paid"),
    "REJECTED": ("red", "✘", "Payment blocked", "blocked before payment"),
    "ESCALATED": ("yellow", "▲", "Held for human review", "held for human review"),
    "DUPLICATE": ("blue", "≡", "Already paid, not paid again", "double payments prevented"),
    "ERROR": ("magenta", "!", "Could not process this invoice", "could not be processed"),
}
ACTION_COLORS = {"approve": "green", "reject": "red", "escalate": "yellow"}
CHECK_LABELS = dict(zip(CHECK_CODES, ("Vendor", "Items", "Due date", "Known items", "Qty", "Stock", "Math")))
STAGE_LABELS = {"load": "read", "vp_review": "VP review"}  # every other node shows its own name
MODE_NOTES = {"live": "calls xAI Grok", "record": "calls Grok, caches new answers", "replay": "recorded answers only"}
CARD_WIDTH = 100  # cards stop growing on wider terminals


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _badge(label: str, color: str) -> Text:
    return Text(f" {label} ", style=f"bold white on {color}")


def _bullets(items: Iterable[str | Text], marker: str = "•", style: str = "") -> Table:
    """A list with hanging indents, so a wrapped item stays aligned under its own text."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, style=style)
    grid.add_column(style=style)
    for item in items:
        grid.add_row(marker, Text(item) if isinstance(item, str) else item)
    return grid


def _claim(state: RunState) -> tuple[str, Decimal | None, str]:
    """(invoice key, amount due, currency); the key is the file when the invoice has no number."""
    number, currency = (state.invoice.invoice_number, state.invoice.currency) if state.invoice else (None, None)
    amount = state.validation.amount if state.validation else None
    return invoice_key(number) or state.invoice_path, amount, currency or "USD"


def _header(inv: Invoice, state: RunState) -> Text:
    """'INV-1004  rev R1  ·  Vendor  ·  Due 2026-02-22', then the vendor address, notes and any extraction retry."""
    line = Text(inv.invoice_number or "No invoice number", style="bold" if inv.invoice_number else "bold red")
    line.append(f"  rev {inv.revision}" if inv.revision else "", style="cyan")
    line.append("  ·  ").append(inv.vendor_name or "Vendor missing", style="bold" if inv.vendor_name else "bold red")
    due = parse_date(inv.due_date)
    line.append("  ·  ").append(f"Due {due or inv.due_date or '—'}", style="" if due else "bold red")
    if inv.vendor_address:
        line.append("\n" + inv.vendor_address.replace("\n", ", "), style="dim")
    if inv.notes:
        line.append(f"\nNote: {inv.notes}", style="italic yellow")
    if state.extract_problems:  # an extraction attempt was rejected and fed back, and a later one passed
        n, why = state.extract_attempts, textwrap.shorten(state.extract_problems[0], 160, placeholder="…")
        line.append(f"\nExtraction self-corrected on attempt {n} (attempt {n - 1}: {why})", style="cyan")
    return line


def _items(inv: Invoice, validation: ValidationReport | None) -> RenderableType:
    """The line items against catalog price and stock, then the totals as printed."""
    cur, stock = inv.currency or "USD", {s.sku: s for s in validation.stock} if validation else {}
    table = Table(box=box.SIMPLE_HEAD, expand=True, show_edge=False, pad_edge=False, header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Item", ratio=1)  # takes the spare width; every other column fits its content
    table.add_column("SKU")
    for name in ("Qty", "Price", "Catalog", "Stock"):
        table.add_column(name, justify="right")
    for i, line in enumerate(inv.line_items, start=1):
        s = stock.get(line.sku) if line.sku else None
        catalog = s.unit_price if s else None
        # The catalog is priced in USD, so only a USD price can be compared with it.
        off_catalog = cur == "USD" and None not in (catalog, line.unit_price) and line.unit_price != catalog
        qty = "—" if line.quantity is None else f"{line.quantity.normalize():f}"  # 20, 2.5, -5
        short = s is not None and (s.in_stock <= 0 or s.requested > s.in_stock)
        table.add_row(
            Text(str(i), style="dim"),
            Text.assemble(line.item, (f"  {line.note}" if line.note else "", "dim italic")),
            Text(line.sku or "unknown", style="" if line.sku else "bold red"),
            Text(qty, style="bold red" if line.quantity is None or line.quantity <= 0 else ""),
            Text(money(line.unit_price, cur), style="yellow" if off_catalog else ""),
            Text(money(catalog), style="dim"),
            Text(str(s.in_stock) if s else "—", style="bold red" if short else "green" if s else "dim"),
        )
    tax = f"Tax {inv.tax_rate}" if inv.tax_rate else "Tax"
    printed = [("Subtotal", inv.subtotal), (tax, inv.tax_amount), ("Shipping", inv.shipping), ("Total", inv.total)]
    totals = Text(justify="right")
    for label, value in printed:
        if value is not None or label == "Total":
            totals.append(f"{'  ·  ' if totals else ''}{label} ", style="dim")
            totals.append(money(value, cur), style="bold" if label == "Total" else "")
    return Group(table, totals) if inv.line_items else totals


def _checklist(validation: ValidationReport) -> RenderableType:
    """One chip per check category, then each failure."""
    failed = {FAILURE_CATEGORY[c.code] for c in validation.failures}
    chips = Text("  ").join(
        Text(f"✗ {label}", style="bold red") if code in failed else Text(f"✓ {label}", style="green")
        for code, label in CHECK_LABELS.items()
    )
    failures = [Text.assemble((c.code, "bold"), "  ", c.message) for c in validation.failures]
    return Group(chips, _bullets(failures, "✗", "red")) if failures else chips


def lookup(call: ToolCall) -> Text:
    """'payment_history(INV-1004) → no prior payment', '… → paid before: $1,890.00 to Acme', or its error."""
    result = call.result if isinstance(call.result, dict) else {}
    if "payments" in result:
        paid = [f"{money(Decimal(str(p['amount'])), p['currency'])} to {p['vendor']}" for p in result["payments"]]
        outcome = "paid before: " + "; ".join(paid) if paid else "no prior payment"
    else:
        outcome = str(result.get("error", call.result))
    query = ", ".join(str(v) for v in call.args.values())
    return Text.assemble((f"{call.name}({query})", "cyan"), (" → ", "dim"), outcome)


def _vp_rounds(state: RunState) -> list[RenderableType]:
    """Each VP round (action badge, lookups, rationale, concerns, the critic's verdict), then a guardrail override."""
    rounds: list[RenderableType] = []
    for n, decision in enumerate(state.vp_decisions, start=1):
        heading = f"VP review · round {n}" if len(state.vp_decisions) > 1 else "VP review"
        badge = _badge(decision.action.upper(), ACTION_COLORS[decision.action])
        parts: list[RenderableType] = [Text.assemble((heading, "bold"), "  ", badge)]
        if lookups := [lookup(c) for c in state.tool_calls if c.round == n]:
            parts.append(_bullets(lookups, "↳"))
        parts.append(Text(decision.rationale))
        if decision.concerns:
            parts.append(_bullets(decision.concerns, "•", "yellow"))
        if n <= len(state.critiques):
            critique = state.critiques[n - 1]
            verdict = ("✓ accepted", "green") if critique.verdict == "accept" else ("✗ revise", "bold yellow")
            parts.append(Text.assemble(("Critic  ", "bold"), verdict))
            if critique.problems:
                parts.append(_bullets(critique.problems, "•", "yellow"))
        rounds.append(Group(*parts))
    if state.overridden and state.decision:  # the guardrail did not follow the VP
        escalate = state.route == "escalate"
        why, style = ("the critic still objects", "bold yellow") if escalate else ("checks failed", "bold red")
        rounds.append(Text(f"Guardrail override: the VP said {state.decision.action}, but {why}.", style=style))
    return rounds


def _verdict(state: RunState) -> Panel:
    status = state.status or "ERROR"
    color, icon, headline, _ = STATUS[status]
    head = Text.assemble(_badge(f"{icon} {status}", color), (f"  {headline}", f"bold {color}"))
    _, amount, currency = _claim(state)
    head.append(f"  ·  {money(amount, currency)}" if amount is not None else "", style="bold")
    on_card = {c.message for c in state.validation.failures} if state.validation else set()
    reasons = [r for r in state.reasons if r not in on_card]  # failed checks are already listed on the card
    body: list[RenderableType] = [head, _bullets(reasons)] if reasons else [head]
    if state.payment_ref and not any(state.payment_ref in r for r in reasons):  # not already quoted by a reason
        body.append(Text.assemble(("Payment ref ", "dim"), (state.payment_ref, f"bold {color}")))
    return Panel(Group(*body), border_style=color, box=box.HEAVY, padding=(0, 1))


def render_result(state: RunState, console: Console) -> None:
    """Print one invoice's card, its verdict banner and a timings footer (just the banner if nothing was extracted)."""
    width, title = min(console.width, CARD_WIDTH), Text(Path(state.invoice_path).name, style="bold")
    verdict = _verdict(state)
    console.print()
    if state.invoice is None:
        verdict.title, verdict.title_align = title, "left"
    else:
        sections: list[RenderableType] = [_header(state.invoice, state), _items(state.invoice, state.validation)]
        sections += [_checklist(state.validation)] if state.validation else []
        body = [part for section in sections + _vp_rounds(state) for part in (Text(), section)][1:]  # blank-line gaps
        card = Panel(Group(*body), title=title, title_align="left", border_style="dim", padding=(0, 1))
        console.print(card, width=width)
    console.print(verdict, width=width)
    t = state.timings_ms  # per node, plus the "total" that process_invoice adds
    footer = [f"{t['total'] / 1000:.2f} s"] if "total" in t else []
    # Extraction's Grok time comes from its calls: the first one ran on main.py's pool before this invoice's turn.
    extract_ms = sum(c.get("duration_ms", 0) for c in state.llm_calls if c.get("task") == "extract")
    t = {**t, "extract": extract_ms} if extract_ms else t
    footer += [f"{STAGE_LABELS.get(n, n)} {t[n] / 1000:.2f} s" for n in ("extract", "vp_review", "critic") if n in t]
    console.print(Text(" · ".join(footer), style="dim"))


def summary_rows(states: list[RunState]) -> list[tuple[str, int, str, str]]:
    """(status, file count, amount per currency, note) for every status; also used by the dashboard."""
    rows = []
    for status, (_, _, _, note) in STATUS.items():
        group = [s for s in states if (s.status or "ERROR") == status]
        claims = {_claim(s) for s in group}  # an invoice sent as PDF and text counts once; a revision does not
        sums: dict[str, Decimal] = {}
        for _, amount, currency in claims:
            if amount is not None and amount > 0:  # a negative "amount due" must not shrink the total
                sums[currency] = sums.get(currency, Decimal(0)) + amount
        total = " + ".join(money(v, c) for c, v in sorted(sums.items(), key=lambda kv: (kv[0] != "USD", kv[0])))
        if len(claims) != len(group):
            note += f" · {_plural(len(claims), 'invoice')}"
        rows.append((status, len(group), total, note))
    return rows


def render_summary(states: list[RunState], console: Console) -> None:
    """Print a row per status: its file count, its amount per currency and a note."""
    grid = Table.grid(padding=(0, 2))
    for justify in ("left", "right", "right"):
        grid.add_column(justify=justify, no_wrap=True)  # type: ignore[arg-type]
    grid.add_column()  # the note wraps on a narrow terminal, so status, count and amount stay whole
    for status, count, total, note in summary_rows(states):
        color = STATUS[status][0]
        style = f"bold {color}" if count else "dim"
        badge = _badge(status, color) if count else Text(f" {status} ", style="dim")
        grid.add_row(badge, Text(str(count), style=style), Text(total, style=style), Text(note, style="dim"))
    console.print()
    console.print(Text(f"Run summary · {_plural(len(states), 'file')}", style="bold"))
    console.print(grid)


def render_intro(console: Console, *, mode: str, model: str, db_path: str, run_id: str, count: int,
                 workers: int) -> None:
    """Three lines up front, so the user knows how Grok will be used before anything runs."""
    files = _plural(count, "file") + (f" · {_plural(min(workers, count), 'worker')}" if count > 1 else "")
    llm = f"{mode} · {model} · {MODE_NOTES.get(mode, mode)}"
    console.print(Text.assemble((f"{'Acme invoice agent':<20}", "bold"), (f"run {run_id} · {files}", "dim")))
    console.print(Text.assemble((f"{'LLM':<20}", "dim"), (llm, "cyan")))
    console.print(Text.assemble((f"{'Inventory DB':<20}", "dim"), db_path))


@contextmanager
def stage_progress(console: Console, filename: str, index: int, total: int) -> Iterator[StageFn]:
    """Show '⠋ [3/20] invoice_1004.json  ✓ read · ✓ extract' while the block runs (``index`` is 1-based) and
    yield ``on_stage(node, state)`` for ``graph.process_invoice``. The line is erased at the end and never drawn
    off a terminal. ``on_stage`` swallows its own errors: a display must never break a run."""
    label = Text(f"[{index}/{total}] {filename}  ", style="bold")
    status = console.status(label)
    stages: list[Text] = []

    def on_stage(node: str, state: RunState) -> None:
        try:  # ✗ marks a stage that failed or sent the invoice back: a rejected extraction, a critique to revise
            bad = (node == "extract" and state.invoice is None) or (state.error is not None and node != "finalize")
            bad = bad or (node == "critic" and bool(state.critiques) and state.critiques[-1].verdict == "revise")
            stages.append(Text.assemble(("✗ ", "red") if bad else ("✓ ", "green"), STAGE_LABELS.get(node, node)))
            # The last three stages fit in 80 columns next to the longest sample file name.
            shown = stages if len(stages) <= 3 else [Text("…", style="dim"), *stages[-3:]]
            line = label + Text(" · ", style="dim").join(shown)
            line.truncate(console.width - 2, overflow="ellipsis")  # 2: the spinner and a space
            status.update(line)
        except Exception:  # noqa: BLE001 - see the docstring
            pass

    if not console.is_terminal:
        yield on_stage
        return
    with status:
        yield on_stage


def result_to_dict(state: RunState) -> dict[str, Any]:
    """The run state as JSON-safe data (Decimals as strings) without the document text, plus the final decision."""
    data = state.model_dump(mode="json", exclude={"source_text"})
    data["decision"] = state.decision.model_dump(mode="json") if state.decision else None
    return data
