"""Review dashboard: ``streamlit run dashboard.py`` (after a ``main.py`` run).

Shows the latest result per invoice file (main.py saves them in the inventory DB) with a $ summary and, like the
terminal card, each invoice's details: lines vs catalog and stock, checks, the VP's rounds and the critic. Each
ESCALATED invoice gets Approve / Reject: approve goes through ``payment.pay`` (USD only, never twice); reject pays
nothing. Both are logged to that run's JSONL log and ``audit_log`` as stage "review".
"""

import json
import re
from pathlib import Path

import streamlit as st

from invoice_agent import db, payment, ui
from invoice_agent.log import RunLogger
from invoice_agent.models import RunState
from invoice_agent.validate import FAILURE_CATEGORY, invoice_key, money, parse_date

DB = db.db_path()
BANNERS = {"PAID": st.success, "DUPLICATE": st.info, "ESCALATED": st.warning, "REJECTED": st.error, "ERROR": st.error}
ACTION_COLORS = {"approve": "green", "reject": "red", "escalate": "orange"}


def plain(text: str) -> str:
    """Invoice and Grok text shown as-is: markdown escaped, so '$5 ... $6' is not read as LaTeX."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|$<>~])", r"\\\1", str(text))


def bullets(items: list[str], color: str = "") -> str:
    return "\n".join(f"- :{color}[{plain(i)}]" if color else f"- {plain(i)}" for i in items)


def decide(run_id: str, state: RunState, action: str) -> str:
    """Apply a reviewer's approve/reject to an ESCALATED invoice, save it and log it."""
    if action == "approve":
        status, ref, reason = payment.pay(state.invoice, state.validation.amount, DB)
    else:
        status, ref, reason = "REJECTED", None, "Rejected by a reviewer"
    updated = state.model_copy(update={"status": status, "payment_ref": ref, "reasons": [reason]})
    db.save_result(state.invoice_path, run_id, status, json.dumps(ui.result_to_dict(updated), ensure_ascii=False), DB)
    logger = RunLogger(run_id, db_path=DB).bind(file=Path(state.invoice_path).name)
    number = invoice_key(state.invoice.invoice_number)
    logger.event("review", reason, invoice_number=number, action=action, status=status)
    return f"{Path(state.invoice_path).name}: {status}. {reason}"


def show_invoice(s: RunState) -> None:
    """What the terminal card shows: header, notes, lines vs catalog and stock, totals, checks, VP rounds, verdict."""
    inv, val = s.invoice, s.validation
    if inv is not None:
        cur, due = inv.currency or "USD", parse_date(inv.due_date)
        rev = f" · rev {inv.revision}" if inv.revision else ""
        number, vendor = inv.invoice_number or "No invoice number", inv.vendor_name or "Vendor missing"
        due_text = plain(due or inv.due_date or "—")
        st.markdown(f"**{plain(number)}**{plain(rev)} · {plain(vendor)} · due {due_text} · {cur}")
        if inv.vendor_address:
            st.caption(plain(inv.vendor_address))
        if inv.notes:
            st.info(f"**Note on the invoice:** {plain(inv.notes)}")
        if s.extract_problems:
            st.caption(plain(f"Extraction self-corrected on attempt {s.extract_attempts}: {s.extract_problems[0]}"))
        stock = {x.sku: x for x in val.stock} if val else {}
        rows = []
        for i, line in enumerate(inv.line_items, start=1):
            x = stock.get(line.sku) if line.sku else None
            short = x is not None and (x.in_stock <= 0 or x.requested > x.in_stock)
            need = f" (need {x.requested.normalize():f})" if short else ""
            in_stock = "—" if x is None else f"{x.in_stock}{need}"
            rows.append({
                "#": i, "Item": line.item + (f" ({line.note})" if line.note else ""), "SKU": line.sku or "unknown",
                "Qty": "—" if line.quantity is None else f"{line.quantity.normalize():f}",
                "Price": money(line.unit_price, cur), "Catalog": money(x.unit_price) if x else "—",
                "Line total": money(line.line_total, cur),
                "Stock": in_stock,
            })
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
        tax = f"Tax {inv.tax_rate}" if inv.tax_rate else "Tax"
        totals = [(n, v) for n, v in (("Subtotal", inv.subtotal), (tax, inv.tax_amount), ("Shipping", inv.shipping),
                                      ("Total", inv.total)) if v is not None or n == "Total"]
        st.markdown(" · ".join(f"{plain(n)} **{plain(money(v, cur))}**" for n, v in totals))
    if val is not None:
        failed = {FAILURE_CATEGORY[c.code] for c in val.failures}
        st.markdown("  ".join(f":red[✗ {label}]" if code in failed else f":green[✓ {label}]"
                              for code, label in ui.CHECK_LABELS.items()))
        if val.failures:
            st.markdown("\n".join(f"- :red[**{c.code}**] {plain(c.message)}" for c in val.failures))
    for n, d in enumerate(s.vp_decisions, start=1):
        heading = f"VP review · round {n}" if len(s.vp_decisions) > 1 else "VP review"
        st.markdown(f"**{heading}** :{ACTION_COLORS[d.action]}-badge[{d.action.upper()}]")
        for call in (c for c in s.tool_calls if c.round == n):
            st.markdown(f"↳ {plain(ui.lookup(call).plain)}")
        st.markdown(plain(d.rationale))
        if d.concerns:
            st.markdown(bullets(d.concerns, "orange"))
        if n <= len(s.critiques):
            c = s.critiques[n - 1]
            st.markdown("**Critic** " + (":green[✓ accepted]" if c.verdict == "accept" else ":orange[✗ revise]"))
            if c.problems:
                st.markdown(bullets(c.problems, "orange"))
    if s.overridden and s.decision:
        why = "the critic still objects" if s.route == "escalate" else "checks failed"
        st.markdown(f":red[**Guardrail override:** the VP said {s.decision.action}, but {why}.]")
    amount = money(s.validation.amount, inv.currency) if s.validation and inv else ""
    ref = f"\n\nPayment ref **{plain(s.payment_ref)}**" if s.payment_ref else ""
    BANNERS[s.status or "ERROR"](f"**{s.status}** {plain(amount)}\n\n{bullets(s.reasons)}{ref}")


st.set_page_config(page_title="Acme invoices", layout="wide")
st.title("Acme invoices")
db.ensure_db(DB)
results = [(row["run_id"], RunState.model_validate(json.loads(row["data_json"]))) for row in db.get_results(DB)]
if not results:
    st.info(f"No results in {DB} yet. Run `python main.py --invoice_path=data/invoices` first.")
    st.stop()
if "flash" in st.session_state:
    st.success(plain(st.session_state.pop("flash")))

for column, (status, count, total, note) in zip(st.columns(5), ui.summary_rows([s for _, s in results])):
    column.metric(status, count)
    column.caption(plain(f"{total} · {note}" if total else note))

queue = [(run_id, s) for run_id, s in results if s.status == "ESCALATED"]
st.subheader(f"Needs review ({len(queue)})")
for run_id, s in queue:
    inv, name = s.invoice, Path(s.invoice_path).name
    with st.container(border=True):
        st.markdown(f"#### {plain(name)} · {plain(inv.vendor_name or 'no vendor')} · "
                    f"{plain(money(s.validation.amount, inv.currency))}")
        st.warning(f"**Why it's held for a person**\n\n{bullets(s.reasons)}")
        approve, reject = st.columns(2)
        for button, action, label, kind in ((approve, "approve", "Approve and pay", "primary"),
                                            (reject, "reject", "Reject", "secondary")):
            if button.button(label, key=f"{action}:{s.invoice_path}", type=kind, width="stretch"):
                st.session_state["flash"] = decide(run_id, s, action)
                st.rerun()
        with st.expander("Invoice details", expanded=True):
            show_invoice(s)

st.subheader("All invoices")
st.dataframe(
    [
        {
            "File": Path(s.invoice_path).name,
            "Invoice": s.invoice.invoice_number if s.invoice else "",
            "Vendor": s.invoice.vendor_name if s.invoice else "",
            "Amount": money(s.validation.amount, s.invoice.currency) if s.validation else "",
            "Status": s.status,
            "Reason": s.reasons[0] if s.reasons else "",
        }
        for _, s in results
    ],
    hide_index=True,
    width="stretch",
)
by_name = {Path(s.invoice_path).name: s for _, s in results}
if choice := st.selectbox("Open an invoice", list(by_name), index=None, placeholder="Pick a file to see its details"):
    with st.container(border=True):
        show_invoice(by_name[choice])
