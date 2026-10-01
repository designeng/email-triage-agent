"""Semantic memory: facts about the user's world.

Two ways to write facts, both into ("email_assistant", <user_id>, "collection"):
  * hot path   - the response agent gets manage_memory / search_memory tools and
                 decides itself what to remember and what to look up;
  * background - `consolidate_facts()` runs a LangMem memory manager over a
                 finished conversation; it may also update / delete stale facts,
                 which keeps the collection free of duplicates and contradictions.
"""

from __future__ import annotations

from typing import Sequence

from langchain_core.messages import AnyMessage
from langgraph.store.base import BaseStore
from langmem import create_manage_memory_tool, create_memory_store_manager, create_search_memory_tool

from email_assistant.config import APP, memory_model, ns

# `{langgraph_user_id}` is substituted by LangMem from config["configurable"].
NAMESPACE = (APP, "{langgraph_user_id}", "collection")

MANAGE_INSTRUCTIONS = """\
Proactively call this tool when you:
1. Learn who a person is or how they relate to the user (role, team, client, family).
2. Receive an explicit request to remember something or to change your behavior.
3. Notice a durable preference or recurring arrangement (e.g. "no meetings on Fridays").
4. Find that an existing memory is incorrect or outdated - then UPDATE or DELETE it
   (pass its id) instead of creating a new, conflicting one.
Store one self-contained fact per memory."""

CONSOLIDATION_INSTRUCTIONS = """\
Extract durable facts about the user's world from this email-handling session:
people and their roles, relationships, preferences, recurring arrangements and commitments.
Skip one-off details that will not matter for future emails.
Merge duplicates, rewrite facts that became outdated, delete facts that were contradicted.
Each memory must be a single self-contained statement."""


def memory_tools(store: BaseStore | None = None) -> list:
    return [
        create_manage_memory_tool(namespace=NAMESPACE, instructions=MANAGE_INSTRUCTIONS, store=store),
        create_search_memory_tool(namespace=NAMESPACE, store=store),
    ]


def consolidate_facts(store: BaseStore, user_id: str, messages: Sequence[AnyMessage]) -> list:
    """Background extraction/dedup of facts after a session (off the hot path)."""
    manager = create_memory_store_manager(
        memory_model(),
        namespace=NAMESPACE,
        instructions=CONSOLIDATION_INSTRUCTIONS,
        enable_inserts=True,
        enable_deletes=True,
        store=store,
    )
    return manager.invoke(
        {"messages": list(messages)}, config={"configurable": {"langgraph_user_id": user_id}}
    )


def list_facts(store: BaseStore, user_id: str, query: str | None = None, limit: int = 50) -> list:
    return store.search(ns(user_id, "collection"), query=query, limit=limit)
