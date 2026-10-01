"""Procedural memory: versioned, self-improving prompts.

Instructions (triage rules + agent instructions) live in the store, not in code:

  ("email_assistant", user, "prompts")                 key=<name> -> {"active", "latest", "history"}
  ("email_assistant", user, "prompt_versions", <name>) key=<n>    -> {"prompt", "source", "note", ...}

The optimizer never overwrites a prompt in place. It writes a *candidate*
version; triage candidates must pass an eval gate (accuracy on a small set of
labelled emails must not drop) before becoming active. Every activation is
recorded in `history`, so `rollback()` is always available.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from langchain_core.messages import AnyMessage
from langgraph.store.base import BaseStore
from langmem import create_multi_prompt_optimizer

from email_assistant.config import memory_model, ns
from email_assistant.prompts import DEFAULT_PROCEDURAL_PROMPTS, PROMPT_UPDATE_HINTS
from email_assistant.schemas import Email

TRIAGE_PROMPTS = ("triage_ignore", "triage_notify", "triage_respond")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PromptRegistry:
    def __init__(self, store: BaseStore, user_id: str):
        self.store, self.user_id = store, user_id

    # ------------------------------------------------------------ storage

    def _meta(self, name: str) -> dict | None:
        item = self.store.get(ns(self.user_id, "prompts"), name)
        return item.value if item else None

    def _put_meta(self, name: str, meta: dict) -> None:
        self.store.put(ns(self.user_id, "prompts"), name, meta, index=False)

    def _put_version(self, name: str, version: int, value: dict) -> None:
        self.store.put(ns(self.user_id, "prompt_versions", name), str(version), value, index=False)

    def version(self, name: str, version: int) -> dict:
        item = self.store.get(ns(self.user_id, "prompt_versions", name), str(version))
        if item is None:
            raise KeyError(f"{name} v{version} not found")
        return item.value

    def _ensure(self, name: str) -> dict:
        meta = self._meta(name)
        if meta is None:
            self._put_version(name, 1, {"prompt": DEFAULT_PROCEDURAL_PROMPTS[name], "source": "default",
                                        "note": "initial", "created_at": _now(), "status": "active"})
            meta = {"active": 1, "latest": 1, "history": [1]}
            self._put_meta(name, meta)
        return meta

    # ------------------------------------------------------------ reading

    def get(self, name: str) -> str:
        meta = self._ensure(name)
        return self.version(name, meta["active"])["prompt"]

    def get_all(self) -> dict[str, str]:
        return {name: self.get(name) for name in DEFAULT_PROCEDURAL_PROMPTS}

    def versions(self, name: str) -> list[tuple[int, dict]]:
        meta = self._ensure(name)
        return [(v, self.version(name, v)) for v in range(1, meta["latest"] + 1)]

    def active_version(self, name: str) -> int:
        return self._ensure(name)["active"]

    # ------------------------------------------------------------ writing

    def propose(self, name: str, prompt: str, *, source: str, note: str = "") -> int:
        """Store a new candidate version (not active yet)."""
        meta = self._ensure(name)
        version = meta["latest"] + 1
        self._put_version(name, version, {"prompt": prompt, "source": source, "note": note,
                                          "created_at": _now(), "status": "candidate"})
        self._put_meta(name, {**meta, "latest": version})
        return version

    def _set_status(self, name: str, version: int, status: str, **extra) -> None:
        self._put_version(name, version, {**self.version(name, version), "status": status, **extra})

    def activate(self, name: str, version: int) -> None:
        meta = self._ensure(name)
        self.version(name, version)  # existence check
        self._set_status(name, meta["active"], "inactive")
        self._set_status(name, version, "active", activated_at=_now())
        self._put_meta(name, {**meta, "active": version, "history": [*meta["history"], version]})

    def reject(self, name: str, version: int, reason: str) -> None:
        self._set_status(name, version, "rejected", reason=reason)

    def rollback(self, name: str) -> int:
        """Re-activate the previously active version."""
        meta = self._ensure(name)
        if len(meta["history"]) < 2:
            raise ValueError(f"{name}: nothing to roll back to")
        history = meta["history"][:-1]
        previous = history[-1]
        self._set_status(name, meta["active"], "rolled_back")
        self._set_status(name, previous, "active")
        self._put_meta(name, {**meta, "active": previous, "history": history})
        return previous


# ---------------------------------------------------------------- eval gate

EVAL_PATH = Path(__file__).resolve().parents[3] / "evals" / "triage_eval.jsonl"


def load_eval_set(path: Path | str = EVAL_PATH) -> list[tuple[Email, str]]:
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    return [(row["email"], row["label"]) for row in rows]


# classify_fn(email, prompts) -> "ignore" | "notify" | "respond"
ClassifyFn = Callable[[Email, dict[str, str]], str]


def triage_accuracy(classify_fn: ClassifyFn, prompts: dict[str, str], eval_set) -> float:
    if not eval_set:
        return 1.0
    hits = sum(classify_fn(email, prompts) == label for email, label in eval_set)
    return hits / len(eval_set)


# ---------------------------------------------------------------- optimizer


@dataclass
class OptimizationReport:
    proposed: dict[str, int] = field(default_factory=dict)
    activated: dict[str, int] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)
    baseline_accuracy: float | None = None
    candidate_accuracy: float | None = None


def optimize_from_feedback(
    store: BaseStore,
    user_id: str,
    messages: Sequence[AnyMessage],
    feedback: str,
    *,
    classify_fn: ClassifyFn | None = None,
    eval_set: list[tuple[Email, str]] | None = None,
    min_delta: float = 0.0,
    optimizer=None,
) -> OptimizationReport:
    """Rewrite prompts from (trajectory, feedback) with LangMem, gated by eval.

    Triage prompt candidates are activated together only if accuracy on
    `eval_set` is >= baseline + min_delta. Agent instruction candidates have no
    automatic metric and are activated directly (use rollback if needed).
    """
    registry = PromptRegistry(store, user_id)
    current = registry.get_all()
    optimizer = optimizer or create_multi_prompt_optimizer(memory_model(), kind="prompt_memory")
    updated = optimizer.invoke(
        {
            "trajectories": [(list(messages), feedback)],
            "prompts": [
                {"name": name, "prompt": text, "when_to_update": PROMPT_UPDATE_HINTS[name],
                 "update_instructions": "Make minimal, targeted edits. Keep the existing rules unless the feedback contradicts them."}
                for name, text in current.items()
            ],
        }
    )

    report = OptimizationReport()
    changed = {p["name"]: p["prompt"] for p in updated if p["prompt"].strip() != current[p["name"]].strip()}
    for name, text in changed.items():
        report.proposed[name] = registry.propose(name, text, source="optimizer", note=feedback[:500])

    triage_changed = [n for n in changed if n in TRIAGE_PROMPTS]
    if triage_changed:
        if classify_fn is not None and eval_set:
            candidate = {**current, **{n: changed[n] for n in triage_changed}}
            report.baseline_accuracy = triage_accuracy(classify_fn, current, eval_set)
            report.candidate_accuracy = triage_accuracy(classify_fn, candidate, eval_set)
            passed = report.candidate_accuracy >= report.baseline_accuracy + min_delta
        else:
            passed = True  # no eval available -> trust the optimizer (rollback still possible)
        for name in triage_changed:
            version = report.proposed[name]
            if passed:
                registry.activate(name, version)
                report.activated[name] = version
            else:
                registry.reject(name, version, reason=f"eval {report.candidate_accuracy:.2f} < "
                                                      f"baseline {report.baseline_accuracy:.2f}")
                report.rejected[name] = version

    if "agent_instructions" in changed:
        registry.activate("agent_instructions", report.proposed["agent_instructions"])
        report.activated["agent_instructions"] = report.proposed["agent_instructions"]
    return report
