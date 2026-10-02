"""HTTP API + Gmail push webhook + static Vue UI (install with `uv sync --extra server`).

    uv run email-assistant serve            # http://127.0.0.1:8000

Routes:
  /api/...              JSON API used by the UI (frontend/)
  /webhooks/gmail       Pub/Sub push endpoint (TOOLS_BACKEND=google)
  /                     built UI from frontend/dist, if present
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from email_assistant.assistant import Assistant
from email_assistant.inbox import Conflict, Inbox, NotFound
from email_assistant.integrations.gmail_push import GmailPush
from email_assistant.memory import semantic
from email_assistant.prompts import DEFAULT_PROCEDURAL_PROMPTS
from email_assistant.schemas import Classification

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_EMAILS = ROOT / "data" / "sample_emails.jsonl"
DIST = ROOT / "frontend" / "dist"

EDITABLE_FIELDS = {"to", "subject", "content"}


class Decision(BaseModel):
    type: Literal["accept", "edit", "response", "ignore"]
    args: dict[str, str] | None = None  # edit: replacement to / subject / content
    text: str | None = None  # response: feedback to the agent

    @field_validator("args")
    @classmethod
    def _only_draft_fields(cls, v):
        if v and not set(v) <= EDITABLE_FIELDS:
            raise ValueError(f"args may only contain {sorted(EDITABLE_FIELDS)}")
        return v


class NewEmail(BaseModel):
    author: str = ""
    to: str = ""
    subject: str
    email_thread: str


class Correction(BaseModel):
    label: Classification


class Feedback(BaseModel):
    text: str


class Activate(BaseModel):
    version: int


def create_app(assistant: Assistant, *, push: GmailPush | None = None, inbox: Inbox | None = None) -> FastAPI:
    inbox = inbox or Inbox(assistant)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if push:
            for target in (push.start, lambda: push.renew_loop(stop)):
                threading.Thread(target=target, daemon=True, name="gmail-push").start()
        yield
        stop.set()
        inbox.close()

    app = FastAPI(title="Email assistant", lifespan=lifespan)

    def or_http(call, *args):
        try:
            return call(*args)
        except NotFound:
            raise HTTPException(404, "email not found")
        except Conflict as exc:
            raise HTTPException(409, str(exc))

    # ------------------------------------------------------------ inbox

    @app.get("/api/status")
    def status():
        return {"user": assistant.user_id, "backend": os.getenv("TOOLS_BACKEND", "mock"),
                "gmail_push": push is not None}

    @app.get("/api/emails")
    def list_emails():
        return inbox.list()

    @app.get("/api/emails/{email_id}")
    def get_email(email_id: str):
        record = inbox.get(email_id)
        if record is None:
            raise HTTPException(404, "email not found")
        return record

    @app.post("/api/emails", status_code=202)
    def add_email(email: NewEmail):
        """Manually feed an email into the pipeline (handy with the mock backend)."""
        return inbox.ingest(email.model_dump())

    @app.get("/api/samples")
    def samples():
        return [json.loads(line) for line in SAMPLE_EMAILS.read_text().splitlines() if line.strip()]

    @app.post("/api/emails/{email_id}/decision", status_code=202)
    def decide(email_id: str, decision: Decision):
        return or_http(inbox.decide, email_id, decision.model_dump(exclude_none=True))

    @app.post("/api/emails/{email_id}/triage-correction")
    def correct_triage(email_id: str, body: Correction):
        return or_http(inbox.correct_triage, email_id, body.label)

    @app.post("/api/emails/{email_id}/feedback")
    def feedback(email_id: str, body: Feedback):
        """Procedural memory: runs the prompt optimizer (slow: several LLM calls + eval gate)."""
        return or_http(inbox.give_feedback, email_id, body.text)

    # ------------------------------------------------------------ memory

    @app.get("/api/prompts")
    def prompts():
        reg = assistant.prompts
        return [
            {"name": name, "active": reg.active_version(name),
             "versions": [{"version": v, **data} for v, data in reg.versions(name)]}
            for name in DEFAULT_PROCEDURAL_PROMPTS
        ]

    @app.post("/api/prompts/{name}/activate")
    def activate_prompt(name: str, body: Activate):
        if name not in DEFAULT_PROCEDURAL_PROMPTS:
            raise HTTPException(404, "unknown prompt")
        try:
            assistant.prompts.activate(name, body.version)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        return {"name": name, "active": body.version}

    @app.post("/api/prompts/{name}/rollback")
    def rollback_prompt(name: str):
        if name not in DEFAULT_PROCEDURAL_PROMPTS:
            raise HTTPException(404, "unknown prompt")
        try:
            return {"name": name, "active": assistant.prompts.rollback(name)}
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/facts")
    def facts(q: str | None = None):
        return [{"id": i.key, "content": i.value.get("content"), "score": i.score}
                for i in semantic.list_facts(assistant.store, assistant.user_id, query=q or None)]

    # ------------------------------------------------------------ Gmail push

    @app.post("/webhooks/gmail", status_code=204)
    async def gmail_webhook(request: Request, background: BackgroundTasks, token: str = ""):
        if push is None or not push.token:
            raise HTTPException(503, "Gmail push is not configured")
        if not hmac.compare_digest(token, push.token):
            raise HTTPException(403, "bad token")
        envelope = await request.json()
        try:
            payload = json.loads(base64.b64decode(envelope["message"]["data"]))
        except (KeyError, ValueError):
            raise HTTPException(400, "not a Pub/Sub push envelope")
        log.info("gmail push: historyId=%s", payload.get("historyId"))
        # Ack right away: Pub/Sub redelivers anything that is not answered with 2xx quickly.
        background.add_task(push.handle_notification)

    if DIST.is_dir():
        app.mount("/", StaticFiles(directory=DIST, html=True), name="ui")
    return app
