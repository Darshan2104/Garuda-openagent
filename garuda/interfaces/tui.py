"""Rich terminal rendering for the interactive chat (Garuda D2).

What is LIVE vs POST-HOC
------------------------
The agent loop (:mod:`garuda.core.loop`) drives the model through
``model.complete()`` — a blocking call that returns a whole turn at once — so
there is no token-by-token stream flowing through the loop yet. To stay honest
about that, this renderer is driven by the EventStore rather than by the model:

* LIVE (per event, as they are emitted during a turn): tool calls, tool
  results, todo-list updates, and the assistant text of each model turn. The
  chat driver in ``cli.py`` runs the agent as a background task and drains new
  events while a "thinking" spinner is shown, so these surface as they happen.
* POST-HOC: nothing extra — the final message is just the last thing rendered.

* NOT YET LIVE: true token-level streaming of assistant text. The streaming
  model API (``Model.stream`` / ``LitellmModel.complete_streaming``) exists and
  is exercised by tests, and ``on_assistant_delta`` accepts incremental chunks,
  but the loop does not feed it token-by-token. Wiring token streaming through
  the loop is a deliberate future step (D3).

The renderer degrades gracefully: if ``rich`` is not installed it falls back to
plain ``print`` output with an identical method surface. It is intentionally
decoupled from the loop — callers push events in via the callback methods, so
this module never imports the agent loop.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

_TODO_MARKS = {"pending": "☐", "in_progress": "▶", "completed": "☑"}


def rich_available() -> bool:
    """Whether the optional ``rich`` dependency can be imported.

    Isolated in a function so tests can monkeypatch it to force the plain path.
    """
    try:
        import rich  # noqa: F401
        from rich.console import Console  # noqa: F401

        return True
    except Exception:
        return False


def _truncate(text: str, limit: int) -> str:
    text = text.rstrip("\n")
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [+{len(text) - limit} chars]"


def _format_args(args: dict[str, Any] | None, limit: int = 160) -> str:
    if not args:
        return ""
    try:
        rendered = json.dumps(args, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(args)
    return _truncate(rendered, limit)


class ChatRenderer:
    """Renders chat events to the terminal, with a rich and a plain backend.

    The public method surface is identical in both modes:
    ``header``, ``on_assistant_delta``, ``on_tool_call``, ``on_tool_result``,
    ``on_todo``, ``on_done`` and the ``thinking`` context manager.
    """

    def __init__(self, use_rich: bool | None = None):
        if use_rich is None:
            use_rich = rich_available()
        self._rich = bool(use_rich)
        self._console = None
        if self._rich:
            try:
                from rich.console import Console

                self._console = Console()
            except Exception:
                self._rich = False

    # -- header ------------------------------------------------------------
    def header(self, model: str, agent: str, workspace: str, session_id: str) -> None:
        if self._rich and self._console is not None:
            from rich.panel import Panel
            from rich.table import Table

            table = Table.grid(padding=(0, 2))
            table.add_column(justify="right", style="dim")
            table.add_column(style="bold")
            table.add_row("agent", agent)
            table.add_row("model", model)
            table.add_row("workspace", workspace)
            table.add_row("session", session_id)
            self._console.print(
                Panel(table, title="[bold cyan]Garuda chat", border_style="cyan", expand=False)
            )
            self._console.print("[dim]Enter a task (empty line to quit).[/dim]\n")
            return
        print(f"Garuda chat — agent={agent} model={model} workspace={workspace}")
        print(f"session={session_id}")
        print("Enter a task (empty line to quit).\n")

    # -- thinking spinner --------------------------------------------------
    def thinking(self, label: str = "Thinking…"):
        if self._rich and self._console is not None:
            return self._console.status(f"[bold cyan]{label}", spinner="dots")
        return self._plain_status(label)

    @staticmethod
    @contextlib.contextmanager
    def _plain_status(label: str):
        print(f"[garuda] {label}", flush=True)
        yield

    # -- assistant text ----------------------------------------------------
    def on_assistant_delta(self, text: str) -> None:
        if not text:
            return
        if self._rich and self._console is not None:
            from rich.markdown import Markdown
            from rich.panel import Panel

            self._console.print(
                Panel(Markdown(text), border_style="green", title="[green]assistant", expand=True)
            )
            return
        print(text, end="", flush=True)

    # -- tool calls / results ---------------------------------------------
    def on_tool_call(self, name: str, args: dict[str, Any] | None) -> None:
        rendered_args = _format_args(args)
        if self._rich and self._console is not None:
            suffix = f" [dim]{rendered_args}[/dim]" if rendered_args else ""
            self._console.print(f"[bold yellow]⚙ {name}[/bold yellow]{suffix}")
            return
        suffix = f" {rendered_args}" if rendered_args else ""
        print(f"\n[tool] {name}{suffix}", flush=True)

    def on_tool_result(self, name: str, content: str, is_error: bool = False) -> None:
        body = _truncate(content or "", 500)
        if self._rich and self._console is not None:
            colour = "red" if is_error else "blue"
            label = "error" if is_error else "result"
            self._console.print(f"  [{colour}]↳ {name} {label}:[/{colour}] {body}")
            return
        tag = "tool-error" if is_error else "tool-result"
        print(f"[{tag}] {name}: {body}", flush=True)

    # -- todo list ---------------------------------------------------------
    def on_todo(self, todos: list[dict[str, Any]]) -> None:
        if not todos:
            return
        if self._rich and self._console is not None:
            from rich.panel import Panel

            lines = [
                f"{_TODO_MARKS.get(item.get('status', 'pending'), '☐')} {item.get('content', '')}"
                for item in todos
            ]
            self._console.print(
                Panel("\n".join(lines), title="[magenta]todo", border_style="magenta", expand=False)
            )
            return
        print("[todo]", flush=True)
        for item in todos:
            mark = _TODO_MARKS.get(item.get("status", "pending"), "☐")
            print(f"  {mark} {item.get('content', '')}", flush=True)

    # -- final message -----------------------------------------------------
    def on_done(self, final: str) -> None:
        final = final or ""
        if self._rich and self._console is not None:
            from rich.markdown import Markdown
            from rich.panel import Panel

            self._console.print(
                Panel(Markdown(final) if final else "[dim](no message)[/dim]",
                      border_style="green", title="[bold green]done", expand=True)
            )
            return
        print(f"\n{final}\n", flush=True)
