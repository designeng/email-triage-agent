from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

Classification = Literal["ignore", "notify", "respond"]


class Email(TypedDict, total=False):
    id: str
    author: str
    to: str
    subject: str
    email_thread: str


class Router(BaseModel):
    """Analyze the unread email and route it according to its content."""

    reasoning: str = Field(description="Step-by-step reasoning behind the classification.")
    classification: Classification = Field(
        description="'ignore' for irrelevant emails, 'notify' for important information "
        "that doesn't need a response, 'respond' for emails that need a reply."
    )


class State(TypedDict, total=False):
    email_input: Email
    triage: dict  # {"classification": ..., "reasoning": ...}
    messages: Annotated[list[AnyMessage], add_messages]


def email_text(email: Email) -> str:
    """Text used as the semantic-search key for an email."""
    return (
        f"From: {email.get('author', '')}\nTo: {email.get('to', '')}\n"
        f"Subject: {email.get('subject', '')}\n{email.get('email_thread', '')}"
    )
