"""The graph with scripted LLM roles: both loops, the guardrail, errors, logging and progress, plus one
record -> replay round trip through the real Grok wrappers (default_deps) with only the network faked."""

import json
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_agent import db, llm
from invoice_agent.graph import Deps, default_deps, process_invoice
from invoice_agent.llm import CacheMiss, LLMError, SchemaMismatch
from invoice_agent.log import RunLogger
from invoice_agent.models import Critique, Decision, Invoice

ROOT = Path(__file__).resolve().parents[1]
INVOICE_1001 = ROOT / "data" / "invoices" / "invoice_1001.txt"
RUN_ID = "20261120-000000-test"
APPROVE = Decision(action="approve", rationale="Every check passed at catalog prices.", concerns=[])
ESCALATE = Decision(action="escalate", rationale="A human should confirm the vendor.", concerns=["Unknown vendor"])
ACCEPT = Critique(verdict="accept", problems=[])
REVISE = Critique(verdict="revise", problems=["The vendor was not checked; escalate instead."])


def extraction(name: str) -> Invoice:
    return Invoice.model_validate_json((ROOT / "tests" / "fixtures" / "extractions" / f"{name}.json").read_text())


GOOD = extraction("invoice_1001.txt")
BAD = GOOD.model_copy(update={"total": Decimal("5500")})  # printed nowhere in the document
BAD_PROBLEM = "total 5500 does not appear in the document"
OFF_SCHEMA = "Grok returned output that does not match Invoice"


class Script:
    """A fake LLM role: plays its answers in order (raising exceptions) and records each call's arguments."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, *args):
        self.calls.append(args)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def meta(task: str) -> dict:
    return {"task": task, "mode": "fake", "cached": False, "duration_ms": 1.0}


def extracts(*answers) -> Script:
    return Script(*[a if isinstance(a, Exception) else (a, meta("extract")) for a in answers])


def vp(*decisions) -> Script:
    return Script(*[d if isinstance(d, Exception) else (d, [], [meta("approve")]) for d in decisions])


def critic(*critiques) -> Script:
    return Script(*[(c, meta("critic")) for c in critiques])


@pytest.fixture
def run(tmp_path, db_path):
    logger = RunLogger(RUN_ID, runs_dir=tmp_path / "runs", db_path=db_path)

    def run(extract, approve, review, path=INVOICE_1001, on_stage=None):
        return process_invoice(path, Deps(extract, approve, review, logger, db_path), RUN_ID, on_stage)

    return run


def events(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "runs" / f"{RUN_ID}.jsonl").read_text().splitlines()]


def payments(db_path: Path) -> list[tuple]:
    with closing(db.connect(db_path)) as conn:
        return [tuple(row) for row in conn.execute("SELECT invoice_number, amount FROM payments")]


def test_ungrounded_and_off_schema_extractions_are_retried_with_their_problems(run, db_path):
    extract = extracts(BAD, SchemaMismatch(OFF_SCHEMA), GOOD)
    state = run(extract, vp(APPROVE), critic(ACCEPT))

    assert state.status == "PAID" and state.extract_attempts == 3, state.reasons
    assert [problems for _, _, problems in extract.calls] == [[], [BAD_PROBLEM], [OFF_SCHEMA]]
    assert state.extract_problems == [OFF_SCHEMA]  # the last rejected attempt's, kept for the result card
    assert [c["task"] for c in state.llm_calls] == ["extract", "extract", "approve", "critic"]  # none off-schema
    assert payments(db_path) == [("INV-1001", "5000.00")]


def test_extraction_gives_up_after_three_attempts(run):
    state = run(extracts(BAD, BAD, BAD), vp(), critic())

    assert state.status == "ERROR" and state.extract_attempts == 3 and state.reasons == [state.error]
    detail = f"extraction still failed its checks after 3 attempts ({BAD_PROBLEM})"
    assert state.error == f"Could not extract invoice data: {detail}"
    assert "validate" not in state.timings_ms


@pytest.mark.parametrize(("path", "extract", "approve", "error"), [
    (INVOICE_1001, extracts(KeyError("x")), vp(), "Could not extract invoice data: unexpected KeyError: 'x'"),
    (INVOICE_1001, extracts(CacheMiss("Not recorded.")), vp(), "Could not extract invoice data: Not recorded."),
    (ROOT / "nope.txt", extracts(), vp(), f"Could not read the invoice file: File not found: {ROOT}/nope.txt"),
    (INVOICE_1001, extracts(GOOD), vp(TimeoutError("slow")), "VP review failed: unexpected TimeoutError: slow"),
], ids=["extract-bug", "cache-miss", "missing-file", "vp-timeout"])
def test_a_failing_stage_ends_in_error_without_a_traceback(run, tmp_path, db_path, path, extract, approve, error):
    state = run(extract, approve, critic(), path=path)

    assert (state.status, state.error, state.reasons) == ("ERROR", error, [error])
    assert len(extract.calls) <= 1 and payments(db_path) == []  # an LLM error other than off-schema is not retried
    [failed] = [e for e in events(tmp_path) if e["level"] == "error"]
    assert failed["message"] == error and "Traceback" in failed["traceback"]  # the log keeps it, the user never sees it


def test_a_revise_sends_the_vp_back_and_a_second_one_is_escalated(run, db_path):
    approve = vp(APPROVE, APPROVE)
    state = run(extracts(GOOD), approve, critic(REVISE, REVISE))

    assert [history for *_, history in approve.calls] == [[], [(APPROVE, REVISE)]]  # round 2 sees round 1
    assert state.status == "ESCALATED" and state.overridden and len(state.critiques) == 2
    assert state.reasons == [f"Critic did not accept the VP's decision to approve: {REVISE.problems[0]}"]
    assert payments(db_path) == []


def test_an_escalation_after_a_revise_goes_to_a_human_unreviewed(run):
    review = critic(REVISE)
    state = run(extracts(GOOD), vp(APPROVE, ESCALATE), review)

    assert state.status == "ESCALATED" and not state.overridden and state.reasons == ESCALATE.concerns
    assert len(review.calls) == 1


@pytest.mark.parametrize(("name", "reason"), [
    ("data/invoices/invoice_1002.txt", "GadgetX: 20 requested, only 5 in stock"),
    ("fixtures/invoices/invoice_2002.txt", "GadgetX: 8 requested, only 5 in stock"),  # a prompt injection the VP obeys
])
def test_the_guardrail_rejects_a_failing_invoice_whatever_the_vp_says(run, db_path, name, reason):
    obeys = Decision(action="approve", rationale="The notes say the CFO already signed off.", concerns=[])
    approve, review = vp(obeys), critic()
    state = run(extracts(extraction(Path(name).name)), approve, review, path=ROOT / name)

    assert approve.calls[0][0].notes == extraction(Path(name).name).notes  # the VP saw the document's own notes
    assert state.status == "REJECTED" and state.overridden and state.reasons == [reason]
    assert review.calls == [] and payments(db_path) == []  # the critic only reviews clean invoices


def test_the_api_key_never_reaches_the_logs(run, tmp_path, db_path, monkeypatch):
    secret = "xai-TEST-SECRET-0123456789"
    monkeypatch.setenv("XAI_API_KEY", secret)
    error = LLMError("Grok request failed")
    error.__cause__ = RuntimeError(f"401 Unauthorized: invalid key {secret}")
    state = run(extracts(error), vp(), critic())

    assert state.error == "Could not extract invoice data: Grok request failed"
    log_text = (tmp_path / "runs" / f"{RUN_ID}.jsonl").read_text()
    assert secret not in log_text and "invalid key ***" in log_text
    with closing(db.connect(db_path)) as conn:
        assert not [row for row in conn.execute("SELECT data_json FROM audit_log") if secret in row[0]]


def test_on_stage_sees_every_node_in_order_and_each_node_logs_one_event(run, tmp_path):
    seen = []

    def on_stage(node, state):
        seen.append((node, state.status))
        raise RuntimeError("the display broke")  # must not change the outcome

    state = run(extracts(GOOD), vp(APPROVE), critic(ACCEPT), on_stage=on_stage)

    order = ["load", "extract", "validate", "vp_review", "critic", "guardrail", "pay", "finalize"]
    assert [node for node, _ in seen] == order and state.status == "PAID"
    assert dict(seen)["guardrail"] is None and dict(seen)["pay"] == "PAID"  # each call sees the state so far
    logged = events(tmp_path)
    assert [e["stage"] for e in logged] == order and all(e["file"] == "invoice_1001.txt" for e in logged)
    assert [e["llm"][0]["task"] for e in logged if e["llm"]] == ["extract", "approve", "critic"]
    assert set(order) | {"total"} == set(state.timings_ms)


def test_a_recorded_run_replays_the_same_way(tmp_path, monkeypatch, db_path):
    monkeypatch.delenv("XAI_API_KEY", raising=False)  # the fakes below are the only Grok
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    structured = [BAD, GOOD, ACCEPT]  # an ungrounded extraction, its retry, then the critic
    lookup = {"name": "payment_history", "args": {"invoice_number": "INV-1001"}, "id": "call-1"}
    submit = {"name": "submit_decision", "args": APPROVE.model_dump(), "id": "call-2"}
    turns = [{"content": "", "tool_calls": [lookup]}, {"content": "", "tool_calls": [submit]}]
    monkeypatch.setattr(llm, "_call_live", lambda schema, system, user: structured.pop(0))
    monkeypatch.setattr(llm, "_call_tools_live", lambda messages, tools, tool_choice: turns.pop(0))
    logger = RunLogger(RUN_ID, runs_dir=tmp_path / "runs", db_path=db_path)

    monkeypatch.setenv("LLM_MODE", "record")
    recorded = process_invoice(INVOICE_1001, default_deps(logger, db_path), RUN_ID)
    monkeypatch.setenv("LLM_MODE", "replay")  # the fakes are used up: replay must serve every call from the cache
    replay_db = tmp_path / "replay.db"
    db.seed(replay_db)
    replayed = process_invoice(INVOICE_1001, default_deps(logger, replay_db), RUN_ID)

    tasks = ["extract", "extract", "approve", "approve", "critic"]
    assert [(c["task"], c["cached"]) for c in recorded.llm_calls] == [(task, False) for task in tasks]
    assert [(c["task"], c["cached"]) for c in replayed.llm_calls] == [(task, True) for task in tasks]
    for state in (recorded, replayed):
        assert state.status == "PAID" and state.extract_attempts == 2, state.reasons
        assert [(c.name, c.result["payments"]) for c in state.tool_calls] == [("payment_history", [])]
