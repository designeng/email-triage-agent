"""Command line entry point.

  email-assistant demo                    scripted walkthrough of all memory types
  email-assistant run  [emails.jsonl]     interactive triage/review/feedback loop
  email-assistant gmail [--watch 60]      process unread Gmail (TOOLS_BACKEND=google)
  email-assistant eval                    triage accuracy of the active prompts
  email-assistant prompts list|show|rollback|activate
  email-assistant facts [query]

Without DATABASE_URL memory lives only for the duration of one command, so
`prompts`, `facts` and `eval` are mostly useful with Postgres.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from email_assistant.assistant import Assistant
from email_assistant.config import open_persistence
from email_assistant.memory import procedural, semantic

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_EMAILS = ROOT / "data" / "sample_emails.jsonl"


def load_emails(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def hr(title: str = "") -> None:
    print(f"\n{'─' * 8} {title} {'─' * max(0, 60 - len(title))}")


def print_result(state: dict) -> None:
    triage = state.get("triage", {})
    print(f"Triage: {triage.get('classification', '?').upper()}  ({triage.get('reasoning', '')[:200]})")
    for m in state.get("messages", [])[1:]:
        for call in getattr(m, "tool_calls", []) or []:
            if call["name"] in ("manage_memory", "search_memory", "schedule_meeting", "check_calendar_availability"):
                print(f"  ↳ {call['name']}({json.dumps(call['args'], ensure_ascii=False)[:160]})")


def show_draft(payload: dict) -> None:
    args = payload["args"]
    print(f"\n  ✉  To: {args['to']}\n     Subject: {args['subject']}\n")
    print("     " + args["content"].replace("\n", "\n     "))


def demo_review(payload: dict) -> dict:
    show_draft(payload)
    print("  [auto-accepted]")
    return {"type": "accept"}


def interactive_review(payload: dict) -> dict:
    show_draft(payload)
    while True:
        choice = input("\n  [a]ccept / [e]dit / [f]eedback to agent / [i]gnore > ").strip().lower()
        if choice in ("a", ""):
            return {"type": "accept"}
        if choice == "e":
            print("  New body, finish with an empty line:")
            lines = iter(input, "")
            return {"type": "edit", "args": {"content": "\n".join(lines)}}
        if choice == "f":
            return {"type": "response", "text": input("  feedback > ")}
        if choice == "i":
            return {"type": "ignore"}


def after_email(assistant: Assistant, email: dict, state: dict) -> None:
    """Ask the user for corrections/feedback and feed them into memory."""
    label = state["triage"]["classification"]
    fix = input(f"\nTriage was '{label}'. Correct it? [enter=ok / ignore / notify / respond] > ").strip()
    if fix in ("ignore", "notify", "respond") and fix != label:
        assistant.correct_triage(email, fix)
        print("  saved to episodic memory")
    feedback = input("Feedback on how I handled it (enter to skip) > ").strip()
    if feedback:
        report = assistant.give_feedback(state, feedback)
        print(f"  prompt optimizer: {report}")
    if os.getenv("CONSOLIDATE_FACTS") == "1":
        assistant.consolidate_facts(state)


# ----------------------------------------------------------------- commands


def cmd_demo(assistant: Assistant, _args) -> None:
    emails = {e["id"]: e for e in load_emails(SAMPLE_EMAILS)}

    hr("1. Baseline: triage + response agent")
    for key in ("spam-1", "deploy-1", "question-1"):
        print(f"\n> {emails[key]['subject']}")
        print_result(assistant.process(emails[key], demo_review))

    hr("2. Semantic memory: facts saved in the hot path")
    print_result(assistant.process(emails["intro-1"], demo_review))
    print_result(assistant.process(emails["followup-1"], demo_review))
    for item in semantic.list_facts(assistant.store, assistant.user_id):
        print(f"  fact: {item.value.get('content')}")

    hr("3. Episodic memory: learning from a triage correction")
    digest, digest2 = emails["digest-1"], emails["digest-2"]
    first = assistant.process(digest, demo_review)
    print_result(first)
    print("  user: this kind of digest should be 'notify'")
    assistant.correct_triage(digest, "notify")
    print_result(assistant.process(digest2, demo_review))

    hr("4. Procedural memory: prompt rewritten from feedback")
    state = assistant.process(emails["meeting-1"], demo_review)
    print_result(state)
    feedback = ("Too formal. Write short, casual replies, sign them just 'Alex', "
                "and when scheduling always propose 30-minute meetings.")
    print(f"  user feedback: {feedback}")
    report = assistant.give_feedback(state, feedback, use_eval=False)
    print(f"  {report}")
    print(f"\n  agent_instructions now (v{assistant.prompts.active_version('agent_instructions')}):\n")
    print("    " + assistant.prompts.get("agent_instructions").replace("\n", "\n    "))
    print_result(assistant.process(emails["meeting-2"], demo_review))


def cmd_run(assistant: Assistant, args) -> None:
    for email in load_emails(Path(args.file) if args.file else SAMPLE_EMAILS):
        hr(email.get("subject", ""))
        print(f"From: {email.get('author')}\n{email.get('email_thread', '')[:600]}")
        state = assistant.process(email, interactive_review)
        print_result(state)
        after_email(assistant, email, state)


def cmd_gmail(assistant: Assistant, args) -> None:
    from email_assistant.tools import get_backend

    backend = get_backend()
    if not hasattr(backend, "fetch_unread"):
        sys.exit("Set TOOLS_BACKEND=google to read Gmail")
    while True:
        for email in backend.fetch_unread():
            hr(email["subject"])
            state = assistant.process(email, interactive_review)
            print_result(state)
            backend.mark_read(email["id"])
            if not args.watch:
                after_email(assistant, email, state)
        if not args.watch:
            break
        time.sleep(args.watch)


def cmd_eval(assistant: Assistant, _args) -> None:
    eval_set = procedural.load_eval_set()
    prompts = assistant.prompts.get_all()
    for email, label in eval_set:
        got = assistant._classify_with(email, prompts)
        print(f"{'✓' if got == label else '✗'} {label:8} {got:8} {email['subject']}")
    print(f"accuracy: {procedural.triage_accuracy(assistant._classify_with, prompts, eval_set):.2f}")


def cmd_prompts(assistant: Assistant, args) -> None:
    reg = assistant.prompts
    names = [args.name] if args.name else list(reg.get_all())
    if args.action == "list":
        for name in names:
            print(f"{name}: active v{reg.active_version(name)}")
            for v, data in reg.versions(name):
                print(f"   v{v} [{data['status']}] {data['source']} {data.get('note', '')[:70]}")
    elif args.action == "show":
        for name in names:
            v = args.version or reg.active_version(name)
            print(f"── {name} v{v}\n{reg.version(name, v)['prompt']}\n")
    elif args.action == "rollback":
        print(f"{args.name}: active is now v{reg.rollback(args.name)}")
    elif args.action == "activate":
        reg.activate(args.name, args.version)
        print(f"{args.name}: active is now v{args.version}")


def cmd_facts(assistant: Assistant, args) -> None:
    for item in semantic.list_facts(assistant.store, assistant.user_id, query=args.query):
        print(f"[{item.key[:8]}] {item.value.get('content')}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="email-assistant")
    parser.add_argument("--user", default=os.getenv("USER_ID", "default-user"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo").set_defaults(fn=cmd_demo)
    p = sub.add_parser("run")
    p.add_argument("file", nargs="?")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("gmail")
    p.add_argument("--watch", type=int, default=0, help="poll every N seconds (auto mode)")
    p.set_defaults(fn=cmd_gmail)
    sub.add_parser("eval").set_defaults(fn=cmd_eval)
    p = sub.add_parser("prompts")
    p.add_argument("action", choices=["list", "show", "rollback", "activate"])
    p.add_argument("name", nargs="?")
    p.add_argument("version", nargs="?", type=int)
    p.set_defaults(fn=cmd_prompts)
    p = sub.add_parser("facts")
    p.add_argument("query", nargs="?")
    p.set_defaults(fn=cmd_facts)
    args = parser.parse_args(argv)

    if args.command == "prompts" and args.action in ("rollback", "activate") and not args.name:
        parser.error("prompt name is required")
    if args.command == "prompts" and args.action == "activate" and not args.version:
        parser.error("version is required")

    with open_persistence() as (store, checkpointer):
        args.fn(Assistant(store, checkpointer, args.user), args)


if __name__ == "__main__":
    main()
