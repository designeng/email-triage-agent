"""Gmail push notifications: Gmail -> Pub/Sub topic -> HTTPS webhook -> Inbox.

Setup (once):
  1. Create a Pub/Sub topic and grant `gmail-api-push@system.gserviceaccount.com`
     the Publisher role on it.
  2. Create a *push* subscription on the topic pointing at
     https://<public host>/webhooks/gmail?token=<PUBSUB_VERIFICATION_TOKEN>
  3. Set GMAIL_PUBSUB_TOPIC=projects/<project>/topics/<topic>.

Gmail's notification only says "mailbox changed, here is the new historyId". We
keep our own history cursor in the store and ask Gmail what was added since.
`users.watch` expires after about 7 days, so `renew_loop` re-registers it.
"""

from __future__ import annotations

import logging
import threading

from email_assistant.config import ns
from email_assistant.inbox import Inbox

log = logging.getLogger(__name__)

RENEW_EVERY_SECONDS = 6 * 3600


class GmailPush:
    def __init__(self, backend, inbox: Inbox, *, topic: str | None = None, token: str | None = None):
        self.backend, self.inbox, self.topic, self.token = backend, inbox, topic, token
        self._ns = ns(inbox.user_id, "gmail")
        self._lock = threading.Lock()  # Pub/Sub may deliver notifications concurrently

    # ------------------------------------------------------------ cursor

    def _history_id(self) -> str | None:
        item = self.inbox.store.get(self._ns, "sync")
        return item.value["history_id"] if item else None

    def _save_history_id(self, history_id: str) -> None:
        self.inbox.store.put(self._ns, "sync", {"history_id": str(history_id)}, index=False)

    # ------------------------------------------------------------ operations

    def register_watch(self) -> None:
        if not self.topic:
            return
        res = self.backend.watch(self.topic)
        log.info("gmail watch registered, expires at %s", res.get("expiration"))
        if self._history_id() is None:
            self._save_history_id(res["historyId"])

    def catch_up(self) -> int:
        """Ingest everything unread (startup, or when the history cursor went stale)."""
        cursor = self.backend.current_history_id()  # taken first so nothing arriving meanwhile is missed
        count = sum(self.inbox.ingest(email) is not None for email in self.backend.fetch_unread())
        self._save_history_id(cursor)
        return count

    def start(self) -> None:
        self.register_watch()
        self.catch_up()

    def handle_notification(self) -> int:
        """Called by the webhook: ingest messages added since our cursor."""
        with self._lock:
            cursor = self._history_id()
            if cursor is None:
                return self.catch_up()
            try:
                ids, latest = self.backend.new_message_ids(cursor)
            except Exception as exc:
                if getattr(getattr(exc, "resp", None), "status", None) == 404:  # cursor too old
                    return self.catch_up()
                raise
            count = 0
            for message_id in ids:
                try:
                    count += self.inbox.ingest(self.backend.get_email(message_id)) is not None
                except Exception:
                    log.exception("could not fetch message %s", message_id)
            self._save_history_id(latest)
            return count

    def renew_loop(self, stop: threading.Event) -> None:
        while not stop.wait(RENEW_EVERY_SECONDS):
            try:
                self.register_watch()
            except Exception:
                log.exception("gmail watch renewal failed")
