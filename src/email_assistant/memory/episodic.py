"""Episodic memory: past triage decisions used as few-shot examples.

Stores "email -> correct classification" pairs (mostly the user's corrections)
in ("email_assistant", <user_id>, "examples"). Before triage, the most similar
past emails are retrieved and injected into the router prompt, so the router
learns from corrections without fine-tuning.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from langgraph.store.base import BaseStore, SearchItem

from email_assistant.config import ns
from email_assistant.prompts import FEW_SHOT_TEMPLATE
from email_assistant.schemas import Classification, Email, email_text


def add_example(
    store: BaseStore, user_id: str, email: Email, label: Classification, *, source: str = "correction"
) -> str:
    key = email.get("id") or str(uuid.uuid4())
    store.put(
        ns(user_id, "examples"),
        key,
        {
            "email": dict(email),
            "label": label,
            "source": source,
            "text": email_text(email),
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        index=["text"],  # embed only the email text, not the label/metadata
    )
    return key


def search_examples(store: BaseStore, user_id: str, email: Email, limit: int = 5) -> list[SearchItem]:
    return store.search(ns(user_id, "examples"), query=email_text(email), limit=limit)


def format_few_shot(examples: list) -> str:
    if not examples:
        return "(no examples yet)"
    blocks = []
    for item in examples:
        email = item.value["email"]
        blocks.append(
            FEW_SHOT_TEMPLATE.format(
                subject=email.get("subject", ""),
                author=email.get("author", ""),
                to=email.get("to", ""),
                email_thread=email.get("email_thread", "")[:1500],
                label=item.value["label"],
            )
        )
    return "\n\n".join(blocks)
