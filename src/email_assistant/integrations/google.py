"""Gmail + Google Calendar backend (install with `uv sync --extra google`).

Setup: create an OAuth "Desktop app" client in Google Cloud Console, enable the
Gmail and Calendar APIs, download the client JSON to GOOGLE_CREDENTIALS
(default: credentials.json). The first run opens a browser for consent and
caches the token in GOOGLE_TOKEN (default: token.json).
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Any, Iterator
from zoneinfo import ZoneInfo

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]


def load_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = os.getenv("GOOGLE_TOKEN", "token.json")
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            os.getenv("GOOGLE_CREDENTIALS", "credentials.json"), SCOPES
        )
        creds = flow.run_local_server(port=0)
    with open(token_path, "w") as f:
        f.write(creds.to_json())
    return creds


@dataclass
class GoogleBackend:
    gmail: Any
    calendar: Any
    tz: str = "UTC"
    workday: tuple[int, int] = (9, 18)

    @classmethod
    def from_env(cls) -> "GoogleBackend":
        from googleapiclient.discovery import build

        creds = load_credentials()
        return cls(
            gmail=build("gmail", "v1", credentials=creds),
            calendar=build("calendar", "v3", credentials=creds),
            tz=os.getenv("TIMEZONE", "UTC"),
        )

    # ------------------------------------------------------------- mail

    def send_email(self, to: str, subject: str, content: str) -> str:
        msg = MIMEText(content)
        msg["to"], msg["subject"] = to, subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        sent = self.gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Email sent to {to} (id {sent['id']})"

    def fetch_unread(self, query: str = "is:unread in:inbox", limit: int = 20) -> Iterator[dict]:
        """Polling entry point: yields emails in the graph's `email_input` format."""
        res = self.gmail.users().messages().list(userId="me", q=query, maxResults=limit).execute()
        for ref in res.get("messages", []):
            m = self.gmail.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in m["payload"].get("headers", [])}
            yield {
                "id": m["id"],
                "author": headers.get("from", ""),
                "to": headers.get("to", ""),
                "subject": headers.get("subject", ""),
                "email_thread": _plain_body(m["payload"]) or m.get("snippet", ""),
            }

    def mark_read(self, message_id: str) -> None:
        self.gmail.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()

    # ------------------------------------------------------------- calendar

    def schedule_meeting(self, attendees, subject, duration_minutes, day, start_time) -> str:
        start = datetime.fromisoformat(f"{day}T{start_time}").replace(tzinfo=ZoneInfo(self.tz))
        end = start + timedelta(minutes=duration_minutes)
        event = {
            "summary": subject,
            "start": {"dateTime": start.isoformat(), "timeZone": self.tz},
            "end": {"dateTime": end.isoformat(), "timeZone": self.tz},
            "attendees": [{"email": a} for a in attendees],
        }
        created = self.calendar.events().insert(
            calendarId="primary", body=event, sendUpdates="all"
        ).execute()
        return f"Meeting '{subject}' scheduled: {created.get('htmlLink')}"

    def availability(self, day: str) -> str:
        zone = ZoneInfo(self.tz)
        d = datetime.fromisoformat(day).replace(tzinfo=zone)
        start, end = d.replace(hour=self.workday[0]), d.replace(hour=self.workday[1])
        busy = (
            self.calendar.freebusy()
            .query(body={"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                         "timeZone": self.tz, "items": [{"id": "primary"}]})
            .execute()["calendars"]["primary"]["busy"]
        )
        free, cursor = [], start
        for b in busy:
            b_start = datetime.fromisoformat(b["start"]).astimezone(zone)
            b_end = datetime.fromisoformat(b["end"]).astimezone(zone)
            if b_start > cursor:
                free.append((cursor, b_start))
            cursor = max(cursor, b_end)
        if cursor < end:
            free.append((cursor, end))
        if not free:
            return f"No free time on {day}"
        slots = ", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in free)
        return f"Free slots on {day} ({self.tz}): {slots}"


def _plain_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="replace")
    for part in payload.get("parts", []) or []:
        if text := _plain_body(part):
            return text
    return ""
