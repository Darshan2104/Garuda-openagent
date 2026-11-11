"""Terminal rendering shared by native runs and interactive chat.

TTY runs attach the loop's delta consumer for a transient assistant preview;
completed response events replace it with the authoritative text. Chat still
drains completed-turn events without attaching a token consumer.

The renderer degrades gracefully: if ``rich`` is not installed it falls back to
plain ``print`` output with an identical method surface. It is intentionally
decoupled from the loop — callers push events in via the callback methods, so
this module never imports the agent loop.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from time import monotonic
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
    ``header``, ``on_assistant_delta``, ``on_model_delta``, ``on_assistant_response``,
    ``discard_assistant``, ``on_tool_call``, ``on_tool_result``, ``on_todo``,
    ``on_done`` and the ``thinking`` context manager.
    """

    def __init__(self, use_rich: bool | None = None, stream=None):
        if use_rich is None:
            use_rich = rich_available()
        self._rich = bool(use_rich)
        # Where the plain backend writes. Defaults to stdout; the JSONL chat mode
        # passes stderr so stdout stays a pure event stream. Resolved lazily on
        # each write so tests that swap sys.stdout still capture output.
        self._stream = stream
        self._console = None
        self._assistant_text = ""
        self._assistant_live = None
        self._assistant_refreshed: float | None = None
        self.rendered_assistant_text: set[str] = set()
        if self._rich:
            try:
                from rich.console import Console

                self._console = Console(file=stream) if stream is not None else Console()
            except Exception:
                self._rich = False

    def _out(self, text: str, end: str = "\n") -> None:
        """Write human-facing text to the renderer's stream."""
        print(text, end=end, flush=True, file=self._stream or sys.stdout)

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
        self._out(f"Garuda chat — agent={agent} model={model} workspace={workspace}")
        self._out(f"session={session_id}")
        self._out("Enter a task (empty line to quit).\n")

    # -- thinking spinner --------------------------------------------------
    def thinking(self, label: str = "Thinking…"):
        if self._rich and self._console is not None:
            return self._console.status(f"[bold cyan]{label}", spinner="dots")
        return self._plain_status(label)

    @contextlib.contextmanager
    def _plain_status(self, label: str):
        self._out(f"[garuda] {label}")
        yield

    # -- assistant text ----------------------------------------------------
    def on_model_delta(self, delta) -> None:
        if delta.kind == "discard":
            self.discard_assistant()
        elif delta.kind == "text" and delta.text:
            if os.environ.get("TERM", "").lower() in {"", "dumb", "unknown"}:
                return
            console = self._console
            rich_preview = (self._rich and console is not None
                            and console.is_terminal and not console.is_dumb_terminal)
            width, height = (console.size if rich_preview else shutil.get_terminal_size())
            rows = max(1, height - 1) if rich_preview else 1
            # Only the preview needs a tail; the response event owns the full text.
            limit = max(1, width) * rows
            self._assistant_text = (self._assistant_text + delta.text[-limit:])[-limit:]
            now = monotonic()
            if self._assistant_refreshed is not None and now - self._assistant_refreshed < 0.1:
                return
            self._assistant_refreshed = now
            if rich_preview:
                from rich.live import Live
                from rich.text import Text

                lines = Text(self._assistant_text).wrap(console, max(1, width))
                preview = Text("\n").join(lines[-rows:])
                if self._assistant_live is None:
                    self._assistant_live = Live(
                        preview, console=console,
                        auto_refresh=False, transient=True,
                    )
                    self._assistant_live.start(refresh=True)
                else:
                    self._assistant_live.update(preview, refresh=True)
            else:
                # Keep the plain preview on one line so discard can erase it,
                # even without Rich. Allow two terminal cells per character.
                width = max(0, (width - 1) // 2)
                text = "".join(c if c.isprintable() else " " for c in self._assistant_text)
                self._out("\r\033[2K" + (text[-width:] if width else ""), end="")

    def discard_assistant(self) -> None:
        text, live = self._assistant_text, self._assistant_live
        self._assistant_text = ""
        self._assistant_live = None
        self._assistant_refreshed = None
        if live is not None:
            # Rich's stop renders once with overflow visible; clear first so a
            # long discarded answer cannot spill into terminal scrollback.
            try:
                live.update("", refresh=True)
            finally:
                live.stop()
        elif text:
            self._out("\r\033[2K", end="")

    def on_assistant_response(self, text: str | None) -> None:
        self.discard_assistant()
        if text:
            self.on_assistant_delta(text)
            if not self._rich:
                self._out("")
            self.rendered_assistant_text.add(text.strip())

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
        self._out(text, end="")

    # -- tool calls / results ---------------------------------------------
    def on_tool_call(self, name: str, args: dict[str, Any] | None) -> None:
        rendered_args = _format_args(args)
        if self._rich and self._console is not None:
            suffix = f" [dim]{rendered_args}[/dim]" if rendered_args else ""
            self._console.print(f"[bold yellow]⚙ {name}[/bold yellow]{suffix}")
            return
        suffix = f" {rendered_args}" if rendered_args else ""
        self._out(f"\n[tool] {name}{suffix}")

    def on_tool_result(self, name: str, content: str, is_error: bool = False) -> None:
        body = _truncate(content or "", 500)
        if self._rich and self._console is not None:
            colour = "red" if is_error else "blue"
            label = "error" if is_error else "result"
            self._console.print(f"  [{colour}]↳ {name} {label}:[/{colour}] {body}")
            return
        tag = "tool-error" if is_error else "tool-result"
        self._out(f"[{tag}] {name}: {body}")

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
        self._out("[todo]")
        for item in todos:
            mark = _TODO_MARKS.get(item.get("status", "pending"), "☐")
            self._out(f"  {mark} {item.get('content', '')}")

    # -- turn error --------------------------------------------------------
    def on_error(self, message: str) -> None:
        """Report a failed turn without ending the session."""
        if self._rich and self._console is not None:
            from rich.panel import Panel

            self._console.print(
                Panel(message, border_style="red", title="[bold red]turn failed", expand=False)
            )
            return
        self._out(f"\n[error] {message}\n")

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
        self._out(f"\n{final}\n")
