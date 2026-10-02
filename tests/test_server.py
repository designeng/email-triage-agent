"""API + Gmail push tests with fake LLMs (reuses the fakes from test_offline)."""

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from email_assistant.inbox import Inbox
from email_assistant.integrations.gmail_push import GmailPush
from email_assistant.server import create_app
from tests.test_offline import EMAIL_Q, SPAM, agent_script, make, mock_backend, store, tc  # noqa: F401


def wait_for(client, email_id, status, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = client.get(f"/api/emails/{email_id}").json()
        if record["status"] == status:
            return record
        time.sleep(0.02)
    raise AssertionError(f"{email_id} never reached {status}: {record['status']} {record['error']}")


def draft_agent():
    return agent_script(
        AIMessage("", tool_calls=[tc("write_email", {"to": "i", "subject": "s", "content": "Dear Sir"}, 1)]),
        AIMessage("", tool_calls=[tc("write_email", {"to": "i", "subject": "s", "content": "Hey"}, 2)]),
        AIMessage("Done."),
    )


@pytest.fixture
def client(store):
    app = create_app(make(store, agent=draft_agent()))
    with TestClient(app) as c:
        yield c


def test_ignored_email_finishes_without_review(client):
    client.post("/api/emails", json={"subject": SPAM["subject"], "email_thread": SPAM["email_thread"]})
    [record] = client.get("/api/emails").json()
    done = wait_for(client, record["id"], "done")
    assert done["triage"]["classification"] == "ignore"
    assert done["draft"] is None


def test_draft_waits_for_human_then_feedback_then_edit(client, mock_backend):
    client.post("/api/emails", json={"author": EMAIL_Q["author"], "subject": EMAIL_Q["subject"],
                                     "email_thread": EMAIL_Q["email_thread"]})
    email_id = client.get("/api/emails").json()[0]["id"]

    pending = wait_for(client, email_id, "pending_review")
    assert pending["draft"]["content"] == "Dear Sir"
    assert mock_backend.sent == []  # nothing is sent while waiting

    client.post(f"/api/emails/{email_id}/decision", json={"type": "response", "text": "less formal"})
    pending = wait_for(client, email_id, "pending_review")
    assert pending["draft"]["content"] == "Hey"

    client.post(f"/api/emails/{email_id}/decision", json={"type": "edit", "args": {"content": "Hey Ivan"}})
    done = wait_for(client, email_id, "done")
    assert mock_backend.sent == [{"to": "i", "subject": "s", "content": "Hey Ivan"}]
    assert done["reply"] == "Done."


def test_decision_validation(client):
    client.post("/api/emails", json={"subject": SPAM["subject"], "email_thread": SPAM["email_thread"]})
    email_id = client.get("/api/emails").json()[0]["id"]
    wait_for(client, email_id, "done")
    assert client.post(f"/api/emails/{email_id}/decision", json={"type": "accept"}).status_code == 409
    assert client.post(f"/api/emails/{email_id}/decision", json={"type": "accept"}).status_code == 409
    bad = client.post(f"/api/emails/{email_id}/decision", json={"type": "edit", "args": {"bcc": "x"}})
    assert bad.status_code == 422
    assert client.post("/api/emails/nope/decision", json={"type": "accept"}).status_code == 404


def test_triage_correction_and_prompts(client):
    client.post("/api/emails", json={"subject": SPAM["subject"], "email_thread": SPAM["email_thread"]})
    email_id = client.get("/api/emails").json()[0]["id"]
    wait_for(client, email_id, "done")
    assert client.post(f"/api/emails/{email_id}/triage-correction", json={"label": "notify"}).json()[
        "triage_correction"] == "notify"
    prompts = {p["name"]: p for p in client.get("/api/prompts").json()}
    assert prompts["triage_ignore"]["active"] == 1
    assert client.post("/api/prompts/triage_ignore/rollback").status_code == 409


class FakeGmail:
    def __init__(self):
        self.inbox = {"m1": {**EMAIL_Q, "id": "m1"}}
        self.cursor = "10"
        self.watched = None

    def watch(self, topic):
        self.watched = topic
        return {"historyId": self.cursor, "expiration": "1"}

    def current_history_id(self):
        return self.cursor

    def fetch_unread(self):
        yield from self.inbox.values()

    def get_email(self, message_id):
        return self.inbox[message_id]

    def new_message_ids(self, start):
        return [i for i in self.inbox if i not in ("m1",)], "12"


def test_gmail_push_webhook(store):
    gmail = FakeGmail()
    assistant = make(store)
    inbox = Inbox(assistant)
    push = GmailPush(gmail, inbox, topic="projects/p/topics/t", token="secret")
    with TestClient(create_app(assistant, push=push, inbox=inbox)) as client:
        deadline = time.time() + 5
        while not inbox.get("m1") and time.time() < deadline:  # startup catch-up
            time.sleep(0.02)
        assert gmail.watched == "projects/p/topics/t"

        gmail.inbox["m2"] = {**SPAM, "id": "m2"}
        body = {"message": {"data": base64.b64encode(json.dumps({"historyId": "12"}).encode()).decode()}}
        assert client.post("/webhooks/gmail?token=wrong", json=body).status_code == 403
        assert client.post("/webhooks/gmail?token=secret", json=body).status_code == 204
        wait_for(client, "m2", "done")
        assert push._history_id() == "12"
        assert len(client.get("/api/emails").json()) == 2  # m1 was not ingested twice


def test_webhook_disabled_without_push(client):
    assert client.post("/webhooks/gmail?token=x", json={}).status_code == 503
