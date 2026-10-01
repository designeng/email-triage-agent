"""Models, embeddings, store and checkpointer factories.

Everything is configured through environment variables (see .env.example).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator, Sequence

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore, IndexConfig
from langgraph.store.memory import InMemoryStore

load_dotenv()

APP = "email_assistant"

# Main model: triage router + response agent.
MODEL = os.getenv("MODEL", "claude-opus-5-5")
# Model for LangMem internals (prompt optimizer, background fact extractor).
# LangMem/trustcall force `tool_choice`, which Claude Opus 5.5 / Sonnet 5.5 /
# Fable 5.1 reject with a 400, so this must be a model that accepts forced tool use.
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "claude-haiku-4-5")

EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
DATABASE_URL = os.getenv("DATABASE_URL")


def _supports_server_fallbacks(model: str) -> bool:
    return model.startswith(("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"))


def chat_model(effort: str = "medium", model: str | None = None) -> BaseChatModel:
    """ChatAnthropic for the main model.

    Server-side refusal fallbacks are enabled for models that support them:
    if a safety classifier declines the request, the API retries on a fallback
    model instead of returning `stop_reason: "refusal"`.
    """
    model = model or MODEL
    kwargs: dict = {}
    if _supports_server_fallbacks(model) and os.getenv("DISABLE_FALLBACKS") != "1":
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["model_kwargs"] = {"fallbacks": "default"}
    return ChatAnthropic(model=model, max_tokens=16000, effort=effort, **kwargs)


def memory_model() -> BaseChatModel:
    return ChatAnthropic(model=MEMORY_MODEL, max_tokens=8000)


# ---------------------------------------------------------------- embeddings


class LocalEmbeddings:
    """Local multilingual embeddings via fastembed (ONNX, no API key needed).

    Anthropic does not offer an embeddings endpoint; set EMBEDDING_MODEL to
    e.g. "openai:text-embedding-3-small" to use a hosted provider instead.
    """

    def __init__(self, model_name: str):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name)
        self.dims = len(next(iter(self._model.embed(["probe"]))))

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(list(texts))]


@lru_cache
def index_config() -> IndexConfig:
    if ":" in EMBEDDING_MODEL:  # "provider:model" -> langchain init_embeddings
        from langchain.embeddings import init_embeddings

        emb = init_embeddings(EMBEDDING_MODEL)
        dims = len(emb.embed_query("probe"))
        return {"dims": dims, "embed": emb}
    emb = LocalEmbeddings(EMBEDDING_MODEL)
    return {"dims": emb.dims, "embed": emb}


# ---------------------------------------------------------------- persistence


@contextmanager
def open_persistence(
    index: IndexConfig | None = None,
) -> Iterator[tuple[BaseStore, BaseCheckpointSaver]]:
    """Yield (store, checkpointer).

    DATABASE_URL set -> PostgresStore (pgvector) + PostgresSaver,
    otherwise in-memory implementations for prototyping.
    """
    index = index or index_config()
    if not DATABASE_URL:
        yield InMemoryStore(index=index), InMemorySaver()
        return

    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.store.postgres import PostgresStore

    with (
        PostgresStore.from_conn_string(DATABASE_URL, index=index) as store,
        PostgresSaver.from_conn_string(DATABASE_URL) as saver,
    ):
        store.setup()
        saver.setup()
        yield store, saver


def ns(user_id: str, kind: str, *rest: str) -> tuple[str, ...]:
    """Every record is namespaced per user: ("email_assistant", user_id, kind, ...)."""
    return (APP, user_id, kind, *rest)
