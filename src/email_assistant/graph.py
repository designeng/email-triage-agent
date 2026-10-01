"""LangGraph: triage router -> (respond) -> ReAct response agent.

    START -> triage_router --respond--> response_agent -> END
                          \\--ignore / notify--> END

* triage_router reads triage rules from procedural memory and few-shot
  examples from episodic memory;
* response_agent reads its instructions from procedural memory and uses
  semantic-memory tools in the hot path. write_email pauses the graph with
  interrupt() for human review.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_store
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore
from langgraph.types import Command

from email_assistant.config import chat_model, ns
from email_assistant.memory.episodic import format_few_shot, search_examples
from email_assistant.memory.procedural import PromptRegistry
from email_assistant.memory.semantic import memory_tools
from email_assistant.prompts import (
    AGENT_SYSTEM_PROMPT,
    DEFAULT_PROFILE,
    TRIAGE_SYSTEM_PROMPT,
    TRIAGE_USER_PROMPT,
)
from email_assistant.schemas import Email, Router, State, email_text
from email_assistant.tools import BASE_TOOLS


def user_id_from(config: RunnableConfig) -> str:
    try:
        return config["configurable"]["langgraph_user_id"]
    except KeyError:
        raise ValueError('Pass config={"configurable": {"langgraph_user_id": ..., "thread_id": ...}}')


def get_profile(store: BaseStore, user_id: str) -> dict:
    item = store.get(ns(user_id, "profile"), "profile")
    return {**DEFAULT_PROFILE, **(item.value if item else {})}


def set_profile(store: BaseStore, user_id: str, **fields) -> None:
    store.put(ns(user_id, "profile"), "profile", {**get_profile(store, user_id), **fields}, index=False)


def router_model() -> BaseChatModel:
    # json_schema = native structured outputs; avoids forced tool_choice,
    # which Claude Opus 5.5 rejects.
    return chat_model(effort="low").with_structured_output(Router, method="json_schema")


def classify(
    email: Email,
    *,
    store: BaseStore,
    user_id: str,
    router_llm=None,
    prompts: dict[str, str] | None = None,
    use_examples: bool = True,
) -> Router:
    """Triage one email. `prompts` overrides procedural memory (used by the eval gate)."""
    prompts = prompts or PromptRegistry(store, user_id).get_all()
    examples = search_examples(store, user_id, email) if use_examples else []
    system = TRIAGE_SYSTEM_PROMPT.format(
        **get_profile(store, user_id),
        triage_ignore=prompts["triage_ignore"],
        triage_notify=prompts["triage_notify"],
        triage_respond=prompts["triage_respond"],
        examples=format_few_shot(examples),
    )
    user = TRIAGE_USER_PROMPT.format(
        author=email.get("author", ""), to=email.get("to", ""),
        subject=email.get("subject", ""), email_thread=email.get("email_thread", ""),
    )
    llm = router_llm or router_model()
    return llm.invoke([SystemMessage(system), HumanMessage(user)])


def build_graph(
    *,
    store: BaseStore,
    checkpointer: BaseCheckpointSaver | None = None,
    router_llm=None,
    agent_llm: BaseChatModel | None = None,
):
    router_llm = router_llm or router_model()

    def triage_router(state: State, config: RunnableConfig) -> Command:
        email = state["email_input"]
        result = classify(email, store=get_store(), user_id=user_id_from(config), router_llm=router_llm)
        update: dict = {"triage": {"classification": result.classification, "reasoning": result.reasoning}}
        if result.classification == "respond":
            update["messages"] = [HumanMessage(f"Respond to the email:\n\n{email_text(email)}")]
            return Command(goto="response_agent", update=update)
        return Command(goto=END, update=update)

    def agent_prompt(state: dict, config: RunnableConfig) -> list:
        store, user_id = get_store(), user_id_from(config)
        system = AGENT_SYSTEM_PROMPT.format(
            **get_profile(store, user_id),
            instructions=PromptRegistry(store, user_id).get("agent_instructions"),
        )
        return [SystemMessage(system), *state["messages"]]

    response_agent = create_react_agent(
        agent_llm or chat_model(effort="medium"),
        tools=[*BASE_TOOLS, *memory_tools()],
        prompt=agent_prompt,
        name="response_agent",
    )

    builder = StateGraph(State)
    builder.add_node("triage_router", triage_router, destinations=("response_agent", END))
    builder.add_node("response_agent", response_agent)
    builder.add_edge(START, "triage_router")
    return builder.compile(store=store, checkpointer=checkpointer)
