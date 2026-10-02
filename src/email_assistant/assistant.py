"""High-level session API tying the graph and the three memory types together."""

from __future__ import annotations

import json
import uuid
from functools import partial
from typing import Callable

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from langgraph.types import Command

from email_assistant.graph import build_graph, classify, router_model
from email_assistant.memory import episodic, procedural, semantic
from email_assistant.schemas import Classification, Email, email_text

# review_fn(interrupt_payload) -> {"type": "accept" | "edit" | "response" | "ignore", ...}
ReviewFn = Callable[[dict], dict]


def auto_accept(_payload: dict) -> dict:
    return {"type": "accept"}


class Assistant:
    def __init__(
        self,
        store: BaseStore,
        checkpointer: BaseCheckpointSaver,
        user_id: str,
        *,
        router_llm=None,
        agent_llm=None,
    ):
        self.store, self.user_id = store, user_id
        self.router_llm = router_llm or router_model()
        self.graph = build_graph(
            store=store, checkpointer=checkpointer, router_llm=self.router_llm, agent_llm=agent_llm
        )
        self.prompts = procedural.PromptRegistry(store, user_id)

    def _config(self, thread_id: str) -> dict:
        return {"configurable": {"langgraph_user_id": self.user_id, "thread_id": thread_id}}

    # ------------------------------------------------------------ main flow

    def start(self, email: Email, thread_id: str | None = None) -> tuple[str, dict]:
        """Run the graph until it finishes or pauses on a write_email interrupt."""
        thread_id = thread_id or f"{email.get('id') or uuid.uuid4()}-{uuid.uuid4().hex[:6]}"
        return thread_id, self.graph.invoke({"email_input": email}, self._config(thread_id))

    def resume(self, thread_id: str, decision: dict) -> dict:
        """Continue a paused thread with the human's decision."""
        return self.graph.invoke(Command(resume=decision), self._config(thread_id))

    def state(self, thread_id: str) -> dict:
        return self.graph.get_state(self._config(thread_id)).values

    def process(self, email: Email, review: ReviewFn = auto_accept) -> dict:
        """Run the graph on one email, resolving write_email interrupts via `review` (blocking)."""
        thread_id, result = self.start(email)
        while interrupts := result.get("__interrupt__"):
            result = self.resume(thread_id, review(interrupts[0].value))
        return result

    # ------------------------------------------------------------ learning

    def correct_triage(self, email: Email, label: Classification) -> str:
        """Episodic: remember the right classification for this kind of email."""
        return episodic.add_example(self.store, self.user_id, email, label)

    def give_feedback(self, state: dict, feedback: str, *, use_eval: bool = True):
        """Procedural: let the optimizer rewrite prompts based on this session + feedback."""
        classify_fn = partial(self._classify_with, use_examples=False)
        return procedural.optimize_from_feedback(
            self.store, self.user_id, trajectory(state), feedback,
            classify_fn=classify_fn if use_eval else None,
            eval_set=procedural.load_eval_set() if use_eval else None,
        )

    def consolidate_facts(self, state: dict):
        """Semantic (background): extract/deduplicate facts from a finished session."""
        return semantic.consolidate_facts(self.store, self.user_id, trajectory(state))

    def _classify_with(self, email: Email, prompts: dict[str, str], use_examples: bool = False) -> str:
        return classify(email, store=self.store, user_id=self.user_id, router_llm=self.router_llm,
                        prompts=prompts, use_examples=use_examples).classification


def _text(message: AnyMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "\n".join(b.get("text", "") for b in message.content if isinstance(b, dict) and b.get("type") == "text")


def trajectory(state: dict) -> list[AnyMessage]:
    """Readable session transcript for LangMem (email + triage decision + agent turns, no thinking blocks)."""
    triage = state.get("triage", {})
    out: list[AnyMessage] = [
        HumanMessage(f"Incoming email:\n{email_text(state['email_input'])}"),
        AIMessage(f"Triage decision: {triage.get('classification')}\nReasoning: {triage.get('reasoning')}"),
    ]
    for m in state.get("messages", [])[1:]:  # [0] repeats the email
        if isinstance(m, AIMessage):
            calls = "".join(f"\n[tool call] {c['name']}({json.dumps(c['args'], ensure_ascii=False)})"
                            for c in m.tool_calls)
            out.append(AIMessage(_text(m) + calls))
        elif isinstance(m, ToolMessage):
            out.append(HumanMessage(f"[tool result: {m.name}] {_text(m)}"))
        else:
            out.append(HumanMessage(_text(m)))
    return out
