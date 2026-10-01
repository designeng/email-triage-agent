"""Offline tests: fake LLMs + hashed embeddings, no API keys or downloads needed."""

import hashlib
import math
import re

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from email_assistant import tools
from email_assistant.assistant import Assistant
from email_assistant.config import ns
from email_assistant.memory import episodic, procedural, semantic
from email_assistant.schemas import Router


def hashed_embed(texts):
    """Bag of word-hashes; similar texts -> similar vectors."""
    out = []
    for t in texts:
        v = [0.0] * 128
        for w in re.findall(r"\w+", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 128] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1
        out.append([x / n for x in v])
    return out


class FakeRouter:
    """Classifies by keywords, but obeys few-shot examples from the same sender."""

    def __init__(self):
        self.prompts: list[str] = []

    def invoke(self, messages):
        system, user = messages[0].content, messages[1].content
        self.prompts.append(system)
        sender = re.search(r"From: (.*)", user).group(1)
        m = re.search(rf"Email From: {re.escape(sender)}.*?> Triage Result: (\w+)", system, re.S)
        if m:
            return Router(reasoning="matches past example", classification=m.group(1))
        if "newsletter" in user.lower() or "sale" in user.lower():
            return Router(reasoning="bulk mail", classification="ignore")
        if "ALWAYS_NOTIFY" in system and "build" in user.lower():
            return Router(reasoning="rule", classification="notify")
        if "deploy" in user.lower() or "build" in user.lower():
            return Router(reasoning="fyi", classification="ignore" if "BROKEN_RULES" in system else "notify")
        return Router(reasoning="direct question", classification="respond")


class FakeToolModel(GenericFakeChatModel):
    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(messages)
        return super()._generate(messages, *args, **kwargs)


def agent_script(*messages):
    return FakeToolModel(messages=iter(messages), seen=[])


def tc(name, args, i):
    return {"name": name, "args": args, "id": f"call_{i}", "type": "tool_call"}


EMAIL_Q = {"id": "q1", "author": "Ivan <ivan@co.com>", "to": "alex@co.com",
           "subject": "API docs", "email_thread": "Are /auth/refresh docs missing?"}
SPAM = {"id": "s1", "author": "Deals <d@shop.com>", "to": "alex@co.com",
        "subject": "Big sale", "email_thread": "70% off, sale ends tonight"}
DIGEST = {"id": "d1", "author": "Rust Weekly <news@rw.dev>", "to": "alex@co.com",
          "subject": "Rust Weekly #1", "email_thread": "newsletter: async closures"}


@pytest.fixture
def store():
    return InMemoryStore(index={"dims": 128, "embed": hashed_embed})


@pytest.fixture(autouse=True)
def mock_backend(monkeypatch):
    backend = tools.MockBackend()
    monkeypatch.setattr(tools, "get_backend", lambda: backend)
    return backend


def make(store, user="alex", agent=None):
    return Assistant(store, InMemorySaver(), user, router_llm=FakeRouter(),
                     agent_llm=agent or agent_script(AIMessage("done")))


def test_ignore_ends_without_agent(store):
    agent = agent_script(AIMessage("should not run"))
    state = make(store, agent=agent).process(SPAM)
    assert state["triage"]["classification"] == "ignore"
    assert not state.get("messages")
    assert agent.seen == []


def test_respond_uses_memory_tools_and_hitl(store, mock_backend):
    agent = agent_script(
        AIMessage("", tool_calls=[tc("search_memory", {"query": "Ivan"}, 1)]),
        AIMessage("", tool_calls=[tc("manage_memory", {"content": "Ivan owns the auth docs", "action": "create"}, 2)]),
        AIMessage("", tool_calls=[tc("write_email", {"to": "ivan@co.com", "subject": "Re: API docs", "content": "Will fix"}, 3)]),
        AIMessage("Sent."),
    )
    reviews = []
    state = make(store, agent=agent).process(EMAIL_Q, lambda p: reviews.append(p) or {"type": "accept"})

    assert state["triage"]["classification"] == "respond"
    assert reviews[0]["args"]["subject"] == "Re: API docs"
    assert mock_backend.sent == [{"to": "ivan@co.com", "subject": "Re: API docs", "content": "Will fix"}]
    facts = semantic.list_facts(store, "alex")
    assert [f.value["content"] for f in facts] == ["Ivan owns the auth docs"]
    assert semantic.list_facts(store, "bob") == []  # per-user isolation
    # system prompt comes from procedural memory
    assert isinstance(agent.seen[0][0], SystemMessage)
    assert "Keep replies concise" in agent.seen[0][0].content


def test_hitl_feedback_and_ignore(store, mock_backend):
    agent = agent_script(
        AIMessage("", tool_calls=[tc("write_email", {"to": "i", "subject": "s", "content": "Dear Sir"}, 1)]),
        AIMessage("", tool_calls=[tc("write_email", {"to": "i", "subject": "s", "content": "Hey"}, 2)]),
        AIMessage("ok"),
    )
    decisions = iter([{"type": "response", "text": "less formal"}, {"type": "edit", "args": {"content": "Hey Ivan"}}])
    make(store, agent=agent).process(EMAIL_Q, lambda p: next(decisions))
    assert mock_backend.sent == [{"to": "i", "subject": "s", "content": "Hey Ivan"}]
    feedback_msg = agent.seen[1][-1]
    assert "less formal" in feedback_msg.content


def test_episodic_correction_changes_triage(store):
    a = make(store)
    assert a.process(DIGEST)["triage"]["classification"] == "ignore"
    a.correct_triage(DIGEST, "notify")
    digest2 = {**DIGEST, "id": "d2", "subject": "Rust Weekly #2", "email_thread": "newsletter: borrow checker"}
    assert a.process(digest2)["triage"]["classification"] == "notify"
    assert "Triage Result: notify" in a.router_llm.prompts[-1]
    # another user is unaffected
    assert make(store, user="bob").process(digest2)["triage"]["classification"] == "ignore"


def test_prompt_registry_versions_and_rollback(store):
    reg = procedural.PromptRegistry(store, "alex")
    v1 = reg.get("triage_notify")
    v2 = reg.propose("triage_notify", "new rules", source="test")
    assert reg.get("triage_notify") == v1  # candidate is not active
    reg.activate("triage_notify", v2)
    assert reg.get("triage_notify") == "new rules"
    assert reg.rollback("triage_notify") == 1
    assert reg.get("triage_notify") == v1
    assert procedural.PromptRegistry(store, "bob").get("triage_notify") == v1


class FakeOptimizer:
    def __init__(self, changes):
        self.changes, self.inputs = changes, []

    def invoke(self, payload):
        self.inputs.append(payload)
        return [{**p, "prompt": self.changes.get(p["name"], p["prompt"])} for p in payload["prompts"]]


EVAL = [({"author": "CI <ci@co.com>", "to": "a", "subject": "build failed", "email_thread": "build"}, "notify"),
        (SPAM, "ignore"), (EMAIL_Q, "respond")]


@pytest.mark.parametrize("new_rule,accepted", [("ALWAYS_NOTIFY builds", True), ("BROKEN_RULES", False)])
def test_optimizer_eval_gate(store, new_rule, accepted):
    a = make(store)
    reg = a.prompts
    state = a.process(EMAIL_Q)
    opt = FakeOptimizer({"triage_notify": new_rule, "agent_instructions": "Be casual, sign as Alex."})
    classify_fn = lambda email, prompts: a._classify_with(email, prompts)
    report = procedural.optimize_from_feedback(store, "alex", [], "fb", classify_fn=classify_fn,
                                               eval_set=EVAL, optimizer=opt)
    assert report.proposed.keys() == {"triage_notify", "agent_instructions"}
    assert ("triage_notify" in report.activated) is accepted
    assert (reg.get("triage_notify") == new_rule) is accepted
    assert reg.get("agent_instructions") == "Be casual, sign as Alex."
    status = reg.version("triage_notify", 2)["status"]
    assert status == ("active" if accepted else "rejected")
    assert state["triage"]["classification"] == "respond"


def test_new_agent_instructions_are_used(store):
    procedural.PromptRegistry(store, "alex").activate(
        "agent_instructions",
        procedural.PromptRegistry(store, "alex").propose("agent_instructions", "SIGN AS ALEX", source="t"),
    )
    agent = agent_script(AIMessage("ok"))
    make(store, agent=agent).process(EMAIL_Q)
    assert "SIGN AS ALEX" in agent.seen[0][0].content


def test_give_feedback_builds_clean_trajectory(store, monkeypatch):
    agent = agent_script(AIMessage([{"type": "thinking", "thinking": "", "signature": "x"},
                                    {"type": "text", "text": "Replied."}]))
    a = make(store, agent=agent)
    state = a.process(EMAIL_Q)
    captured = {}
    monkeypatch.setattr(procedural, "create_multi_prompt_optimizer",
                        lambda *a, **k: captured.setdefault("opt", FakeOptimizer({})))
    a.give_feedback(state, "too formal", use_eval=False)
    messages = captured["opt"].inputs[0]["trajectories"][0][0]
    assert "Triage decision: respond" in messages[1].content
    assert messages[-1].content == "Replied."


def test_store_layout(store):
    episodic.add_example(store, "alex", DIGEST, "notify")
    assert store.get(ns("alex", "examples"), "d1").value["label"] == "notify"
