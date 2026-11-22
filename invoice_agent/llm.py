"""Every Grok call: strict structured output (invoke_structured) or one tool-calling turn (invoke_tools).

LLM_MODE picks live (always call Grok), record (serve a recorded response, else call Grok and save it) or
replay (recorded responses only, never the network). Blank means live when XAI_API_KEY is set, else replay.
A recording is LLM_CACHE_DIR/<task>-<key16>.json, keyed on a sha256 of everything sent to Grok.
Settings are read at call time; tests monkeypatch _call_live and _call_tools_live to stay offline.
"""

import hashlib
import json
import os
import time
from collections.abc import Callable
from typing import Any, Literal, TypeVar, cast

from langchain_core.messages import AIMessage, BaseMessage
from langchain_xai import ChatXAI
from pydantic import BaseModel

from invoice_agent import db

T = TypeVar("T", bound=BaseModel)
Mode = Literal["live", "record", "replay"]

REPLAY_MISS_MESSAGE = ("No recorded Grok response for this input (recordings exist only for the sample invoices). "
                       "Set XAI_API_KEY in .env to run live, or use LLM_MODE=record with a key to record one.")
MISSING_KEY_MESSAGE = "XAI_API_KEY is not set. Add it to .env to call Grok, or use LLM_MODE=replay to run offline."
AUTH_FAILED_MESSAGE = "xAI rejected XAI_API_KEY (HTTP {status}). Check the key in .env, or run with LLM_MODE=replay."


class LLMError(Exception):
    """An LLM call failed; the message is short and safe to show to a user."""


class CacheMiss(LLMError):
    """Replay mode has no recorded response for this exact input."""


class SchemaMismatch(LLMError):
    """Grok answered, but not in the requested schema."""


def mode() -> Mode:
    """The active mode. Raises LLMError for an unknown LLM_MODE."""
    value = os.getenv("LLM_MODE", "").strip().lower() or ("live" if _api_key() else "replay")
    if value not in ("live", "record", "replay"):
        raise LLMError(f"LLM_MODE must be one of live, record or replay (got {value!r}).")
    return cast(Mode, value)


def model_name() -> str:
    return os.getenv("XAI_MODEL", "").strip() or "grok-4.3"


def check_key() -> None:
    """Raise LLMError when the mode may call Grok (live or record) and XAI_API_KEY is not set."""
    if mode() != "replay" and not _api_key():
        raise LLMError(MISSING_KEY_MESSAGE)


def redact(text: str) -> str:
    """``text`` with the API key replaced by ***; use it on anything logged or shown."""
    return text.replace(key, "***") if (key := _api_key()) else text


def invoke_structured(task: str, schema: type[T], system: str, user: str) -> tuple[T, dict[str, Any]]:
    """Ask Grok for a ``schema`` instance. Returns (parsed, meta)."""
    request = {"schema": schema.model_json_schema(), "messages": [system, user]}
    output, meta = _cached(task, request, lambda: _call_live(schema, system, user).model_dump(mode="json"))
    return schema.model_validate(output), meta


def invoke_tools(task: str, messages: list[BaseMessage], tools: list[dict], tool_choice: str) -> tuple[AIMessage, dict]:
    """One turn with OpenAI-format ``tools``; ``tool_choice`` is "required" or a tool name. Returns (reply, meta)."""
    sent = [m.model_dump(include={"type", "content", "tool_calls", "tool_call_id"}) for m in messages]  # no run ids
    output, meta = _cached(task, {"tools": tools, "tool_choice": tool_choice, "messages": sent},
                           lambda: _call_tools_live(messages, tools, tool_choice))
    return AIMessage(content=output["content"], tool_calls=output["tool_calls"]), meta


def _cached(task: str, request: dict[str, Any], call: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    """``call()``'s JSON output, or its recording, as the mode says. Returns (output, meta)."""
    current, model, started = mode(), model_name(), time.perf_counter()
    key = hashlib.sha256(json.dumps({"task": task, "model": model, **request}, sort_keys=True).encode()).hexdigest()
    path = db.repo_path(os.getenv("LLM_CACHE_DIR", "").strip() or "fixtures/llm_cache") / f"{task}-{key[:16]}.json"
    if current == "replay" and not path.is_file():
        raise CacheMiss(REPLAY_MISS_MESSAGE)
    cached = current != "live" and path.is_file()
    if cached:
        output = json.loads(path.read_text(encoding="utf-8"))["output"]
    else:
        output = call()
        if current == "record":
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {"task": task, "model": model, "output": output}
            path.write_text(json.dumps(entry, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    return output, {"task": task, "mode": current, "cached": cached, "duration_ms": duration_ms}


def _call_live(schema: type[T], system: str, user: str) -> T:
    """Grok with strict JSON-schema output. Raises SchemaMismatch for an off-schema answer."""
    chat = _chat().with_structured_output(schema, method="json_schema", strict=True)
    try:
        return cast(T, chat.invoke([("system", system), ("human", user)]))
    except ValueError as exc:  # includes pydantic's ValidationError: the OpenAI SDK validates the reply itself
        raise SchemaMismatch(f"Grok returned output that does not match {schema.__name__} ({_describe(exc)})") from exc
    except Exception as exc:
        raise _request_failed(exc) from exc


def _call_tools_live(messages: list[BaseMessage], tools: list[dict], tool_choice: str) -> dict[str, Any]:
    """One Grok turn with ``tools`` bound, as {"content": text, "tool_calls": [{name, args, id}]}.

    A call with unreadable (non-JSON) arguments is dropped, so the VP's next turn has to make it again.
    """
    try:
        reply = _chat().bind_tools(tools, tool_choice=tool_choice).invoke(messages)
    except Exception as exc:
        raise _request_failed(exc) from exc
    return {"content": reply.text, "tool_calls": [{k: c[k] for k in ("name", "args", "id")} for c in reply.tool_calls]}


def _chat() -> ChatXAI:
    if not _api_key():  # ChatXAI's own error for a missing key is less helpful
        raise LLMError(MISSING_KEY_MESSAGE)
    return ChatXAI(model=model_name(), timeout=60, max_retries=2)  # transport retries; graph.py retries bad answers


def _request_failed(exc: Exception) -> LLMError:
    """A rejected key (HTTP 401/403) gets a hint; anything else a one-line, redacted description."""
    status = getattr(exc, "status_code", None)  # set by openai.APIStatusError
    if status in (401, 403):
        return LLMError(AUTH_FAILED_MESSAGE.format(status=status))
    return LLMError(f"Grok request failed ({_describe(exc)})")


def _describe(exc: Exception) -> str:
    return redact(" ".join(f"{type(exc).__name__}: {exc}".split()))


def _api_key() -> str:
    return os.getenv("XAI_API_KEY", "").strip()
