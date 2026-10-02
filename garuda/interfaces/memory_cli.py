"""``garuda memory list | review`` (plan task H.9, #171).

Review is the only way a proposed note becomes memory, and it needs a person at
a terminal: it refuses without one, so a headless run, a script or an agent
cannot accept. Each decision binds the exact proposal digest that was shown.
"""

from __future__ import annotations

import json
import sys

from garuda.context.notes import NotesRefused, ProposalStore

CHOICES = "[a]ccept  [e]dit  [r]eject  [s]kip  [q]uit"


def _show(proposal, out) -> None:
    print(f"\n{proposal.id[:8]}  scope: {proposal.scope}  session: {proposal.session_id}  "
          f"state: {proposal.state}", file=out)
    print(f"  {proposal.text}", file=out)


def review(proposals: ProposalStore, ask, out=None) -> dict:
    """Walk the pending proposals; returns counts. ``ask(prompt) -> str`` is the person."""
    out = out or sys.stdout
    counts = {"accepted": 0, "rejected": 0, "skipped": 0}
    for proposal in proposals.pending():
        _show(proposal, out)
        while True:
            choice = ask(f"{CHOICES}: ").strip().lower()[:1]
            try:
                if choice == "a":
                    path = proposals.accept(proposal.id, digest=proposal.digest)
                    print(f"  added to {path}", file=out)
                    counts["accepted"] += 1
                elif choice == "e":
                    edited = ask("  new text: ")
                    path = proposals.accept(proposal.id, digest=proposal.digest, text=edited)
                    print(f"  added to {path}", file=out)
                    counts["accepted"] += 1
                elif choice == "r":
                    proposals.reject(proposal.id, digest=proposal.digest)
                    counts["rejected"] += 1
                elif choice == "s":
                    counts["skipped"] += 1
                elif choice == "q":
                    return counts
                else:
                    continue
            except NotesRefused as exc:
                print(f"  not done: {exc}", file=out)
                if choice in ("a", "e"):
                    break  # a refused acceptance is not retried blindly
                continue
            break
    return counts


def run_memory(args) -> int:
    from garuda.core.sessions import SessionStore

    try:
        proposals = ProposalStore(SessionStore(), args.workspace)
    except Exception as exc:
        print(f"Error: cannot open the proposal store: {exc}", file=sys.stderr)
        return 2
    if args.memory_command == "list":
        pending = proposals.pending()
        if getattr(args, "json", False):
            print(json.dumps([p.to_dict() for p in pending], indent=2))
        elif not pending:
            print("No pending proposals.")
        for proposal in pending:
            if not getattr(args, "json", False):
                _show(proposal, sys.stdout)
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Error: memory review needs a terminal; a note is only accepted by a person "
              "at one.", file=sys.stderr)
        return 2
    counts = review(proposals, input)
    print(f"\n{counts['accepted']} accepted, {counts['rejected']} rejected, "
          f"{counts['skipped']} skipped.")
    return 0
