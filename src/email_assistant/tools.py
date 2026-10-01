"""Response-agent tools: write_email, schedule_meeting, check_calendar_availability.

The actual side effects are delegated to a backend:
  TOOLS_BACKEND=mock   (default) - in-memory fake mailbox/calendar
  TOOLS_BACKEND=google           - Gmail + Google Calendar (integrations/google.py)

write_email is gated by human-in-the-loop: it calls `interrupt()` and waits for
the user to accept / edit / reject the draft before anything is sent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from langchain_core.tools import tool
from langgraph.types import interrupt


class Backend(Protocol):
    def send_email(self, to: str, subject: str, content: str) -> str: ...
    def schedule_meeting(
        self, attendees: list[str], subject: str, duration_minutes: int, day: str, start_time: str
    ) -> str: ...
    def availability(self, day: str) -> str: ...


@dataclass
class MockBackend:
    sent: list[dict] = field(default_factory=list)
    meetings: list[dict] = field(default_factory=list)

    def send_email(self, to: str, subject: str, content: str) -> str:
        self.sent.append({"to": to, "subject": subject, "content": content})
        return f"Email sent to {to} with subject '{subject}'"

    def schedule_meeting(self, attendees, subject, duration_minutes, day, start_time) -> str:
        self.meetings.append(
            {"attendees": attendees, "subject": subject, "duration": duration_minutes,
             "day": day, "start_time": start_time}
        )
        return f"Meeting '{subject}' scheduled for {day} at {start_time} with {', '.join(attendees)}"

    def availability(self, day: str) -> str:
        return f"Available times on {day}: 9:00 AM, 2:00 PM, 4:00 PM"


@lru_cache
def get_backend() -> Backend:
    if os.getenv("TOOLS_BACKEND", "mock") == "google":
        from email_assistant.integrations.google import GoogleBackend

        return GoogleBackend.from_env()
    return MockBackend()


@tool
def write_email(to: str, subject: str, content: str) -> str:
    """Write and send an email. The user reviews the draft before it is sent."""
    decision = interrupt(
        {"action": "write_email", "args": {"to": to, "subject": subject, "content": content}}
    )
    kind = decision.get("type") if isinstance(decision, dict) else decision
    if kind == "accept":
        return get_backend().send_email(to, subject, content)
    if kind == "edit":
        args = {"to": to, "subject": subject, "content": content, **decision.get("args", {})}
        return get_backend().send_email(**args)
    if kind == "response":
        return (
            "The user did NOT send this draft and left feedback: "
            f"{decision.get('text', '')}\nRevise the email accordingly and call write_email again."
        )
    return "The user decided not to send any email for this thread. Do not retry; finish."


@tool
def schedule_meeting(
    attendees: list[str], subject: str, duration_minutes: int, preferred_day: str, start_time: str
) -> str:
    """Schedule a calendar meeting. preferred_day as YYYY-MM-DD, start_time as HH:MM (24h)."""
    return get_backend().schedule_meeting(attendees, subject, duration_minutes, preferred_day, start_time)


@tool
def check_calendar_availability(day: str) -> str:
    """Check calendar availability for a given day (YYYY-MM-DD)."""
    return get_backend().availability(day)


BASE_TOOLS = [write_email, schedule_meeting, check_calendar_availability]
