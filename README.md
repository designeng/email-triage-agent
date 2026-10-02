# Email assistant with long-term memory

An email assistant made of a triage router plus a ReAct agent, extended with three kinds of long-term memory.
Built with LangGraph, LangMem and Claude.

## Architecture

```mermaid
flowchart TB
    subgraph Input["Input"]
        CLI["CLI: demo / run / gmail"]
        Gmail["Gmail polling<br/>(integrations/google.py)"]
    end

    CLI --> Assistant
    Gmail --> Assistant

    Assistant["Assistant<br/>process / correct_triage /<br/>give_feedback / consolidate_facts"]

    subgraph Graph["LangGraph (graph.py)"]
        direction TB
        START([START]) --> Router["triage_router<br/>Claude, effort=low,<br/>json_schema output"]
        Router -- respond --> Agent["response_agent (ReAct)<br/>Claude, effort=medium"]
        Router -- "ignore / notify" --> END1([END])
        Agent --> END2([END])
    end

    Assistant --> START

    subgraph Tools["Agent tools"]
        direction LR
        WE["write_email<br/>⏸ interrupt() → human review"]
        SM["schedule_meeting"]
        CA["check_calendar_availability"]
        MT["manage_memory / search_memory"]
    end
    Agent <--> Tools
    WE & SM & CA --> Backend["Backend: mock | Google<br/>(Gmail + Calendar)"]

    subgraph Store["Long-term memory: store, namespace (email_assistant, user_id, ...)"]
        direction LR
        Epi[("Episodic<br/>examples")]
        Proc[("Procedural<br/>prompts + prompt_versions")]
        Sem[("Semantic<br/>collection of facts")]
    end

    Epi -- "few-shot examples" --> Router
    Proc -- "triage rules" --> Router
    Proc -- "agent instructions" --> Agent
    MT <--> Sem

    Assistant -- "correct_triage" --> Epi
    Assistant -- "give_feedback → LangMem optimizer<br/>→ candidate → eval gate → activate" --> Proc
    Assistant -- "background consolidation" --> Sem

    Checkpointer[("Checkpointer<br/>InMemorySaver | Postgres")] -.- Graph
```

The store and checkpointer are in-memory by default, or Postgres + pgvector when `DATABASE_URL` is set.

| Memory | Where | Write path | Read path |
|---|---|---|---|
| **Semantic** (facts) | `(app, user, "collection")` | the agent calls `manage_memory` (create/update/delete) in the hot path; `consolidate_facts()` runs in the background with deduplication | the agent calls `search_memory` |
| **Episodic** (examples) | `(app, user, "examples")` | `Assistant.correct_triage(email, label)` | semantic search over similar emails, injected into the router prompt as few-shot examples |
| **Procedural** (prompts) | `(app, user, "prompts")` + `prompt_versions` | `Assistant.give_feedback(state, text)` → LangMem `create_multi_prompt_optimizer` → candidate version → **eval gate** → activate | read from the store on every run |

## Quick start

```bash
uv sync
cp .env.example .env        # set ANTHROPIC_API_KEY
uv run pytest               # offline tests, no key needed
uv run email-assistant demo # scripted walkthrough of all 4 steps
uv run email-assistant run  # interactive: draft review, triage correction, feedback
```

The first run downloads the local multilingual embedding model (~220 MB, fastembed/ONNX).
Anthropic has no embeddings API; to use a hosted one, set `EMBEDDING_MODEL=openai:text-embedding-3-small` or similar.

### Persistent memory (Postgres + pgvector)

```bash
docker compose up -d
uv sync --extra postgres
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/email_assistant
uv run email-assistant --user alex run
uv run email-assistant --user alex prompts list
uv run email-assistant --user alex prompts show agent_instructions 2
uv run email-assistant --user alex prompts rollback agent_instructions
uv run email-assistant --user alex facts "Sarah"
uv run email-assistant --user alex eval
```

Without `DATABASE_URL`, `InMemoryStore` + `InMemorySaver` are used, and memory lives only as long as one command runs.

### Real mail (Gmail + Calendar)

1. In Google Cloud, enable the Gmail API and Calendar API, then create an OAuth client of type **Desktop app** → `credentials.json`.
2. `uv sync --extra google`, set `TOOLS_BACKEND=google` and `TIMEZONE=...`.
3. `uv run email-assistant gmail` processes unread mail once; For continuous operation use `serve` (below).

Every email goes through human-in-the-loop: `write_email` calls `interrupt()`, and you choose
*accept / edit / feedback to agent / ignore*. `email-assistant gmail` does this once, interactively in the terminal.

### Web UI + Gmail push

```bash
uv sync --extra server
(cd frontend && npm install && npm run build)   # once; or `npm run dev` for hot reload on :5173
uv run email-assistant serve                    # http://127.0.0.1:8000
```

fish (no `(...)` subshells):

```fish
uv sync --extra server
cd frontend; and npm install; and npm run build; cd ..
uv run email-assistant serve
```

`serve` runs an API, the Vue UI from `frontend/` and a Gmail webhook in one process. Emails are processed in the
background; a draft waits in the **Inbox** tab (status *needs review*) until you accept / edit / send feedback / reject it,
so no terminal has to stay open on `input()`. The UI also lets you correct triage (episodic memory), send feedback to the
prompt optimizer (procedural memory), browse/activate/roll back prompt versions and search facts. With
`TOOLS_BACKEND=mock` you can add sample emails from the UI.

Instead of polling, Gmail pushes changes through Pub/Sub (`integrations/gmail_push.py`):

1. Create a Pub/Sub topic; give `gmail-api-push@system.gserviceaccount.com` the *Pub/Sub Publisher* role on it.
2. Create a **push** subscription to `https://<public host>/webhooks/gmail?token=<PUBSUB_VERIFICATION_TOKEN>`
   (expose the local port with a tunnel such as `cloudflared`/`ngrok` while developing).
3. Set `GMAIL_PUBSUB_TOPIC`, `PUBSUB_VERIFICATION_TOKEN`, `TOOLS_BACKEND=google` and run `serve`.

On start the server registers `users.watch` (renewed every 6 h; Gmail expires it after ~7 days) and ingests anything
unread. Each notification triggers a `history.list` from a cursor kept in the store; emails are deduplicated by Gmail id.
Without `DATABASE_URL` the inbox, drafts and checkpoints live in memory and vanish on restart.

The server has no login: it binds to `127.0.0.1` by default. Only the webhook is meant to be reachable from outside
(guarded by the token); put the rest behind an authenticating proxy before exposing it.

## Design decisions

- **Two models.** The router and agent run on `claude-opus-5-5` (router at `effort=low`, agent at `medium`), with server-side refusal fallbacks enabled.
  LangMem (trustcall, optimizers, extractor) always forces `tool_choice`, which Opus 5.5 / Sonnet 5.5 / Fable 5.1 reject with a 400, so LangMem gets its own `MEMORY_MODEL` (default `claude-haiku-4-5`).
- **The router uses `method="json_schema"`** (native structured outputs) rather than function calling, for the same reason.
- **Prompts are versioned.** The optimizer never overwrites the active version. Each change becomes a candidate.
  Triage rules are activated only if accuracy on `evals/triage_eval.jsonl` does not drop (the eval checks the rules alone, without few-shot examples).
  `agent_instructions` has no automatic metric, so it is activated directly and can be rolled back with `prompts rollback`.
- **Deduplicating facts.** The manage tool can `update`/`delete` by id, and the prompt tells the agent to update a fact rather than duplicate it.
  The background `consolidate_facts()` (`CONSOLIDATE_FACTS=1`) uses `enable_deletes=True`.
- **Isolation.** Every key starts with `("email_assistant", user_id, ...)`, and `user_id` is passed through `config["configurable"]["langgraph_user_id"]`.

## Layout

```
src/email_assistant/
  config.py            models, embeddings, store/checkpointer (memory | postgres)
  prompts.py           prompt templates + initial procedural-memory values
  schemas.py           Router, State, Email
  tools.py             write_email (HITL), schedule_meeting, check_calendar_availability; mock backend
  integrations/google.py   Gmail/Calendar backend + polling
  memory/semantic.py   LangMem manage/search tools, background consolidation
  memory/episodic.py   few-shot examples: add/search/format
  memory/procedural.py PromptRegistry (versions, rollback), optimizer + eval gate
  graph.py             LangGraph: triage_router → response_agent
  assistant.py         session API: process / start / resume / correct_triage / give_feedback / consolidate_facts
  inbox.py             background processing, drafts parked in the store until a human decides
  server.py            FastAPI: JSON API, Gmail push webhook, serves the built UI
  integrations/gmail_push.py   Pub/Sub notification -> history.list -> Inbox, watch renewal
  cli.py               demo, run, gmail, serve, eval, prompts, facts
frontend/              Vue 3 + Vite UI (inbox/review, prompts, facts)
data/sample_emails.jsonl   demo emails
evals/triage_eval.jsonl    labelled set for the prompt-version gate
tests/                     offline tests (fake LLM, hashed embeddings) incl. API and webhook
```

## Inspiration

The architecture follows the DeepLearning.AI course [Long-Term Agentic Memory with LangGraph](https://www.deeplearning.ai/short-courses/long-term-agentic-memory-with-langgraph/).
