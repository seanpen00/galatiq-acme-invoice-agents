"""The invoice pipeline as a LangGraph StateGraph over RunState: load -> extract (retried while Grok's answer
is off-schema or not grounded in the document) -> validate -> vp_review <-> critic (a "revise" sends the VP
back) -> guardrail -> pay | reject | escalate -> finalize. Every node is timed, logs one event and turns any
exception into ``state.error``, which routes straight to finalize (status ERROR). The three LLM roles come in
through ``Deps``, so tests can run the whole graph offline with fakes.
"""

import dataclasses
import functools
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from invoice_agent import approve, db, ingest, payment, validate
from invoice_agent.llm import LLMError, SchemaMismatch, redact
from invoice_agent.log import RunLogger
from invoice_agent.models import Critique, Decision, Invoice, RunState, ToolCall, ValidationReport

ExtractFn = Callable[[str, str, list[str]], tuple[Invoice, dict]]  # (text, filename, problems)
ApproveFn = Callable[  # (invoice, validation, catalog, history of (decision, critique))
    [Invoice, ValidationReport, dict, list[tuple[Decision, Critique]]], tuple[Decision, list[ToolCall], list[dict]]
]
CriticFn = Callable[[Invoice, ValidationReport, dict, Decision, list[ToolCall]], tuple[Critique, dict]]
StageFn = Callable[[str, RunState], None]  # on_stage(node, state so far)
NodeFn = Callable[[RunState], tuple[dict[str, Any], str]]  # a node returns (state update, log message)

MAX_EXTRACT_ATTEMPTS = 3
MAX_CRITIQUE_ROUNDS = 2  # critiques per invoice; a revise after the last one is escalated
FINALIZE = "finalize"
LABELS = {  # node -> how its error message starts
    "load": "Could not read the invoice file",
    "extract": "Could not extract invoice data",
    "validate": "Validation failed",
    "vp_review": "VP review failed",
    "critic": "Critic review failed",
    "guardrail": "Guardrail failed",
    "pay": "Payment failed",
    "reject": "Recording the rejection failed",
    "finalize": "Finalizing the run failed",
}


@dataclasses.dataclass
class Deps:
    """What the graph needs from outside: the three LLM roles, the run logger and the database."""

    extract: ExtractFn
    approve: ApproveFn
    critic: CriticFn
    logger: RunLogger
    db_path: db.PathArg = None


def default_deps(logger: RunLogger, db_path: db.PathArg = None) -> Deps:
    """Production wiring: Grok extraction, the Grok VP (its lookups read ``db_path``) and the Grok critic."""
    vp = functools.partial(approve.llm_approver, db_path=db_path)
    return Deps(extract=ingest.extract, approve=vp, critic=approve.llm_critic, logger=logger, db_path=db_path)


def _friendly(label: str, exc: Exception) -> str:
    """One line for a failure: our own errors as written, anything else with its type. Never a traceback."""
    detail = str(exc) if isinstance(exc, (ingest.IngestError, LLMError)) else f"unexpected {type(exc).__name__}: {exc}"
    return redact(f"{label}: {detail}")[:400]


def _wrap(name: str, node: NodeFn, logger: RunLogger) -> Callable[[RunState], dict[str, Any]]:
    """Run a node: time it, log one event when it ends and turn any exception into ``error``."""

    def run(state: RunState) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            update, message = node(state)
            details = {key: value for key, value in update.items() if key not in ("source_text", "llm_calls")}
            details["llm"] = update.get("llm_calls", [])[len(state.llm_calls) :]  # this node's calls
        except Exception as exc:  # noqa: BLE001 - every failure becomes a friendly ERROR
            update = {"error": _friendly(LABELS[name], exc)}
            message, details = update["error"], {"traceback": redact(traceback.format_exc())}
        elapsed = round((time.perf_counter() - start) * 1000, 1)
        update["timings_ms"] = {**state.timings_ms, name: round(state.timings_ms.get(name, 0) + elapsed, 1)}
        invoice = update.get("invoice") or state.invoice
        number = validate.invoice_key(invoice.invoice_number) if invoice else None
        level = "error" if update.get("error") else "info"
        logger.event(name, message, invoice_number=number, level=level, duration_ms=elapsed, **details)
        return update

    return run


def _nodes(deps: Deps) -> dict[str, NodeFn]:
    """The nodes. Each runs only after the stages before it succeeded, so the state it reads is set."""

    def load(state: RunState) -> tuple[dict[str, Any], str]:
        text = ingest.read_text(Path(state.invoice_path))
        return {"source_text": text}, f"read {len(text)} characters"

    def extract(state: RunState) -> tuple[dict[str, Any], str]:
        """One attempt. An off-schema or ungrounded answer is retried with its problems; other errors end the run."""
        text, attempt = state.source_text, state.extract_attempts + 1
        update: dict[str, Any] = {"extract_attempts": attempt}
        try:
            invoice, meta = deps.extract(text, Path(state.invoice_path).name, state.extract_problems)
        except SchemaMismatch as exc:
            problems = [str(exc)[:500]]
        else:
            update["llm_calls"] = [*state.llm_calls, meta]
            problems = ingest.ungrounded(invoice, text)
            if not problems:
                return {**update, "invoice": invoice}, f"attempt {attempt} passed its checks"
        update["extract_problems"] = problems  # fed back on the retry; kept for the result card
        if attempt >= MAX_EXTRACT_ATTEMPTS:
            detail = f"extraction still failed its checks after {attempt} attempts ({'; '.join(problems[:2])})"
            update["error"] = f"{LABELS['extract']}: {detail}"[:400]
        return update, update.get("error", f"attempt {attempt} rejected: {problems[0]}")

    def validate_node(state: RunState) -> tuple[dict[str, Any], str]:
        invoice, report = validate.validate(state.invoice, db.get_catalog(deps.db_path))
        failed = ", ".join(c.code for c in report.failures)
        return {"invoice": invoice, "validation": report}, f"failed: {failed}" if failed else "all checks passed"

    def vp_review(state: RunState) -> tuple[dict[str, Any], str]:
        history = list(zip(state.vp_decisions, state.critiques))  # earlier rounds, each with its critique
        catalog = db.get_catalog(deps.db_path)
        decision, calls, metas = deps.approve(state.invoice, state.validation, catalog, history)
        update = {
            "vp_decisions": [*state.vp_decisions, decision],
            "tool_calls": [*state.tool_calls, *calls],
            "llm_calls": [*state.llm_calls, *metas],
        }
        return update, f"VP round {len(update['vp_decisions'])}: {decision.action}"

    def critic(state: RunState) -> tuple[dict[str, Any], str]:
        """Review the latest decision with every lookup so far (a later round may cite an earlier one)."""
        catalog = db.get_catalog(deps.db_path)
        critique, meta = deps.critic(state.invoice, state.validation, catalog, state.decision, state.tool_calls)
        update = {"critiques": [*state.critiques, critique], "llm_calls": [*state.llm_calls, meta]}
        return update, f"critic round {len(update['critiques'])}: {critique.verdict}"

    def guardrail(state: RunState) -> tuple[dict[str, Any], str]:
        # Only a critique of the latest decision counts: after a "revise" the VP may have changed its mind.
        latest = state.critiques[-1] if len(state.critiques) == len(state.vp_decisions) else None
        route, overridden, reasons = payment.guardrail(state.validation, state.decision, latest)
        message = f"VP {state.decision.action} -> {route}" + (" (overridden)" if overridden else "")
        return {"route": route, "overridden": overridden, "reasons": reasons}, message

    def pay(state: RunState) -> tuple[dict[str, Any], str]:
        status, reference, reason = payment.pay(state.invoice, state.validation.amount, deps.db_path)
        update: dict[str, Any] = {"status": status, "payment_ref": reference, "reasons": [reason]}
        if status == "ERROR":  # the bank call failed after the ledger row was written
            update["error"] = reason
        return update, reason

    def reject(state: RunState) -> tuple[dict[str, Any], str]:
        return {"status": "REJECTED"}, "Rejected: " + "; ".join(state.reasons)

    def finalize(state: RunState) -> tuple[dict[str, Any], str]:
        # Only the escalate route arrives here with neither a status nor an error.
        status = "ERROR" if state.error else (state.status or "ESCALATED")
        reasons = [state.error] if state.error else state.reasons
        return {"status": status, "reasons": reasons}, f"{status}: {'; '.join(reasons)}"

    return {
        "load": load, "extract": extract, "validate": validate_node, "vp_review": vp_review, "critic": critic,
        "guardrail": guardrail, "pay": pay, "reject": reject, FINALIZE: finalize,
    }


def _unless_error(node: str) -> Callable[[RunState], str]:
    return lambda state: FINALIZE if state.error else node


def _after_extract(state: RunState) -> str:
    """validate once an attempt passed its checks, else another attempt (the last one sets an error)."""
    if state.error:
        return FINALIZE
    return "validate" if state.invoice is not None else "extract"


def _after_vp(state: RunState) -> str:
    """The critic reviews an approval or rejection of an invoice that passed every check."""
    if state.error:
        return FINALIZE
    reviewed = not state.validation.failures and state.decision.action != "escalate"
    return "critic" if reviewed else "guardrail"


def _after_critic(state: RunState) -> str:
    """A "revise" goes back to the VP while critique rounds remain; then the guardrail escalates it."""
    if state.error:
        return FINALIZE
    revise = state.critiques[-1].verdict == "revise"
    return "vp_review" if revise and len(state.critiques) < MAX_CRITIQUE_ROUNDS else "guardrail"


def _after_guardrail(state: RunState) -> str:
    """pay or reject; an escalation ends the run (a human takes over)."""
    return FINALIZE if state.error or state.route == "escalate" else state.route


def build_graph(deps: Deps) -> CompiledStateGraph:
    graph = StateGraph(RunState)
    for name, node in _nodes(deps).items():
        graph.add_node(name, _wrap(name, node, deps.logger))
    graph.add_edge(START, "load")
    graph.add_conditional_edges("load", _unless_error("extract"), ["extract", FINALIZE])
    graph.add_conditional_edges("extract", _after_extract, ["extract", "validate", FINALIZE])
    graph.add_conditional_edges("validate", _unless_error("vp_review"), ["vp_review", FINALIZE])
    graph.add_conditional_edges("vp_review", _after_vp, ["critic", "guardrail", FINALIZE])
    graph.add_conditional_edges("critic", _after_critic, ["vp_review", "guardrail", FINALIZE])
    graph.add_conditional_edges("guardrail", _after_guardrail, ["pay", "reject", FINALIZE])
    graph.add_edge("pay", FINALIZE)
    graph.add_edge("reject", FINALIZE)
    graph.add_edge(FINALIZE, END)
    return graph.compile()


def process_invoice(path: Path, deps: Deps, run_id: str, on_stage: StageFn | None = None) -> RunState:
    """Run one invoice through the graph. Never raises: any failure ends in status ERROR.

    ``on_stage(node, state so far)`` is called after each node, e.g. to draw a progress line.
    """
    deps = dataclasses.replace(deps, logger=deps.logger.bind(file=Path(path).name))
    state = RunState(run_id=run_id, invoice_path=str(path))
    start = time.perf_counter()
    try:
        config = {"run_name": f"invoice:{Path(path).name}"}  # labels the LangSmith trace, when tracing is on
        for chunk in build_graph(deps).stream(state, config=config, stream_mode="updates"):
            for node, update in chunk.items():  # each node's update replaces the fields it returns
                state = state.model_copy(update=update)
                if on_stage is not None:
                    try:
                        on_stage(node, state)
                    except Exception:  # noqa: BLE001 - a display bug must never change an invoice's outcome
                        pass
    except Exception as exc:  # noqa: BLE001 - graph machinery; the nodes catch their own failures
        message = _friendly("The pipeline failed", exc)
        state = state.model_copy(update={"status": "ERROR", "error": message, "reasons": [message]})
    state.timings_ms["total"] = round((time.perf_counter() - start) * 1000, 1)
    return state
