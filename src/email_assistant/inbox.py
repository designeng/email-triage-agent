"""Asynchronous inbox: runs the graph in the background and parks drafts for human review.

Unlike `Assistant.process` (which blocks on a `review` callback), `Inbox` never
waits for a person. Every email becomes a record in the store:

  ("email_assistant", user, "inbox")  key=<email id> -> record

  status: processing -> pending_review -> processing -> ... -> done | error

A record in `pending_review` holds the draft that `write_email` paused on; the
graph itself is parked in the checkpointer under `thread_id`. Calling
`decide()` resumes it. Because both live in the store/checkpointer, a Postgres
setup survives restarts.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, ToolMessage

from email_assistant.assistant import Assistant, _text
from email_assistant.config import ns
from email_assistant.schemas import Classification, Email

log = logging.getLogger(__name__)


class NotFound(KeyError):
    pass


class Conflict(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actions(state: dict) -> list[dict]:
    """Tool calls of the agent with their results (None while still pending)."""
    actions: dict[str, dict] = {}
    for m in state.get("messages", []):
        if isinstance(m, AIMessage):
            for c in m.tool_calls:
                actions[c["id"]] = {"name": c["name"], "args": c["args"], "result": None}
        elif isinstance(m, ToolMessage) and m.tool_call_id in actions:
            actions[m.tool_call_id]["result"] = _text(m)
    return list(actions.values())


def _reply(state: dict) -> str:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, AIMessage) and not m.tool_calls:
            return _text(m)
    return ""


class Inbox:
    def __init__(self, assistant: Assistant, *, workers: int = 4, consolidate: bool | None = None):
        self.assistant = assistant
        self.store, self.user_id = assistant.store, assistant.user_id
        self.consolidate = os.getenv("CONSOLIDATE_FACTS") == "1" if consolidate is None else consolidate
        self._ns = ns(self.user_id, "inbox")
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="inbox")

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ storage

    def get(self, email_id: str) -> dict | None:
        item = self.store.get(self._ns, email_id)
        return item.value if item else None

    def list(self) -> list[dict]:
        items = self.store.search(self._ns, limit=500)
        return sorted((i.value for i in items), key=lambda r: r["created_at"], reverse=True)

    def _require(self, email_id: str) -> dict:
        record = self.get(email_id)
        if record is None:
            raise NotFound(email_id)
        return record

    def _update(self, email_id: str, **fields) -> dict:
        with self._lock:
            record = {**self._require(email_id), **fields, "updated_at": _now()}
            self.store.put(self._ns, email_id, record, index=False)
            return record

    # ------------------------------------------------------------ main flow

    def ingest(self, email: Email) -> dict | None:
        """Register an email and process it in the background. None if already known."""
        email = dict(email)
        email.setdefault("id", uuid.uuid4().hex)
        with self._lock:
            if self.get(email["id"]):
                return None
            record = {
                "id": email["id"], "email": email, "status": "processing", "thread_id": None,
                "triage": None, "draft": None, "actions": [], "reply": "", "error": None,
                "triage_correction": None, "feedback": [],
                "created_at": _now(), "updated_at": _now(),
            }
            self.store.put(self._ns, email["id"], record, index=False)
        self._pool.submit(self._run, email["id"], lambda: self.assistant.start(email))
        return record

    def decide(self, email_id: str, decision: dict) -> dict:
        """Resume a paused thread with accept / edit / response / ignore."""
        with self._lock:
            record = self._require(email_id)
            if record["status"] != "pending_review":
                raise Conflict(f"email is {record['status']}, not pending_review")
            record = self._update(email_id, status="processing", draft=None)
        thread_id = record["thread_id"]
        self._pool.submit(self._run, email_id, lambda: (thread_id, self.assistant.resume(thread_id, decision)))
        return record

    def _run(self, email_id: str, step) -> None:
        try:
            thread_id, result = step()
            state = self.assistant.state(thread_id)
            fields = {"thread_id": thread_id, "triage": state.get("triage"),
                      "actions": _actions(state), "reply": _reply(state), "error": None}
            if interrupts := result.get("__interrupt__"):
                self._update(email_id, status="pending_review", draft=interrupts[0].value["args"], **fields)
                return
            self._update(email_id, status="done", draft=None, **fields)
            if self.consolidate:
                self.assistant.consolidate_facts(state)
        except Exception as exc:  # surfaced in the UI instead of dying silently in a worker
            log.exception("processing %s failed", email_id)
            self._update(email_id, status="error", error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ learning

    def _state(self, email_id: str) -> tuple[dict, dict]:
        record = self._require(email_id)
        if not record["thread_id"] or record["status"] == "processing":
            raise Conflict("email is still being processed")
        return record, self.assistant.state(record["thread_id"])

    def correct_triage(self, email_id: str, label: Classification) -> dict:
        record = self._require(email_id)
        self.assistant.correct_triage(record["email"], label)
        return self._update(email_id, triage_correction=label)

    def give_feedback(self, email_id: str, text: str) -> dict:
        record, state = self._state(email_id)
        report = self.assistant.give_feedback(state, text)
        self._update(email_id, feedback=[*record["feedback"], text])
        return asdict(report)
