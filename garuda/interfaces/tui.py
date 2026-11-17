"""Terminal rendering shared by native runs and interactive chat.

TTY runs attach the loop's delta consumer for a transient assistant preview;
completed response events replace it with the authoritative text. Interactive
chat drains those events and yields its thinking spinner to the preview. Labelled
child tails share that transient region and never become assistant responses.

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
import weakref
from dataclasses import dataclass
from time import monotonic
from typing import Any

from garuda.model.streaming import CHILD_CALL_END

CHILD_TAIL_CHARS = 512
MAX_CHILD_SOURCES = 8
MAX_CHILD_LINES = 3
CHILD_LABEL_CHARS = 32
_LIVE: weakref.WeakSet[ChatRenderer] = weakref.WeakSet()


@dataclass
class _ChildPreview:
    agent: str
    depth: int
    tail: str
    updated: float


def _printable(text: str) -> str:
    return "".join(c if c.isprintable() else " " for c in text)


def preview_terminal() -> bool:
    """Whether TERM permits transient previews (independent of the output TTY)."""
    return os.environ.get("TERM", "").lower() not in {"", "dumb", "unknown"}


@contextlib.contextmanager
def suspend_previews():
    """Reserve the process's terminal for an approval, including sibling streams."""
    with contextlib.ExitStack() as stack:
        for renderer in list(_LIVE):
            stack.enter_context(renderer.suspended())
        yield


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
    ``on_done`` and the ``thinking`` context manager. Child deltas draw bounded,
    labelled tails; call-end observers clear them before tools or approvals.
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
        self._status = None
        self._children: dict[str, _ChildPreview] = {}
        self._suspended = 0
        self._plain_visible = False
        _LIVE.add(self)
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
    @contextlib.contextmanager
    def thinking(self, label: str = "Thinking…"):
        if self._rich and self._console is not None:
            status = self._console.status(f"[bold cyan]{label}", spinner="dots")
            self._status = status
            try:
                with status:
                    yield
            finally:
                self._status = None
        else:
            with self._plain_status(label):
                yield

    @contextlib.contextmanager
    def _plain_status(self, label: str):
        self._out(f"[garuda] {label}")
        yield

    # -- assistant text ----------------------------------------------------
    def on_model_delta(self, delta) -> None:
        if getattr(delta, "source", None) is not None:
            self._on_child_delta(delta)
            return
        if delta.kind == "discard":
            self.discard_assistant()
        elif delta.kind == "text" and delta.text and preview_terminal():
            console = self._console
            rich_preview = (self._rich and console is not None
                            and console.is_terminal and not console.is_dumb_terminal)
            width, height = (console.size if rich_preview else shutil.get_terminal_size())
            rows = max(1, height - 1) if rich_preview else 1
            limit = max(1, width) * rows
            self._assistant_text = (self._assistant_text + delta.text[-limit:])[-limit:]
            self._draw()

    def _on_child_delta(self, delta) -> None:
        if not preview_terminal():
            return
        if delta.kind == "discard":
            if self._children.pop(delta.source, None) is not None:
                self._draw(force=True)
        elif delta.kind == "text" and delta.text:
            previous = self._children.get(delta.source)
            tail = (previous.tail if previous else "") + _printable(delta.text[-CHILD_TAIL_CHARS:])
            self._children[delta.source] = _ChildPreview(
                _printable((delta.agent or "subagent")[:CHILD_LABEL_CHARS]),
                int(delta.depth), tail[-CHILD_TAIL_CHARS:], monotonic(),
            )
            if len(self._children) > MAX_CHILD_SOURCES:
                oldest = min(self._children, key=lambda source: self._children[source].updated)
                del self._children[oldest]
            self._draw()

    def on_child_event(self, source_id: str, event: dict) -> None:
        """Retire a child's call synchronously, before its tool dispatch."""
        if event.get("type") in CHILD_CALL_END and source_id in self._children:
            del self._children[source_id]
            # Event observers must never interrupt append/tool dispatch, even
            # when a terminal disappears while its last frame is being erased.
            with contextlib.suppress(Exception):
                self._draw(force=True)

    def clear_children(self) -> None:
        if self._children:
            self._children.clear()
            with contextlib.suppress(Exception):
                self._draw(force=True)

    def _close_preview(self) -> None:
        live, self._assistant_live = self._assistant_live, None
        self._assistant_refreshed = None
        if live is not None:
            # Clear before stop: Rich's final overflow render must not spill
            # provisional text into scrollback.
            try:
                live.update("", refresh=True)
            finally:
                live.stop()
        elif self._plain_visible:
            self._out("\r\033[2K", end="")
        self._plain_visible = False

    def _draw(self, force: bool = False) -> None:
        if self._suspended:
            return
        if not self._assistant_text and not self._children:
            self._close_preview()
            if self._status is not None:
                self._status.start()
            return
        if self._status is not None:
            self._status.stop()
        now = monotonic()
        if not force and self._assistant_refreshed is not None and now - self._assistant_refreshed < 0.1:
            return
        self._assistant_refreshed = now
        console = self._console
        rich_preview = (self._rich and console is not None
                        and console.is_terminal and not console.is_dumb_terminal)
        width, height = (console.size if rich_preview else shutil.get_terminal_size())
        children = sorted(self._children.values(), key=lambda child: child.updated, reverse=True)
        if rich_preview:
            from rich.cells import cell_len
            from rich.console import Group
            from rich.live import Live
            from rich.text import Text

            child_lines = []
            for child in children[:MAX_CHILD_LINES]:
                label = f"↳ {child.agent} · depth {child.depth} │ "
                room = max(0, width - cell_len(label))
                tail = child.tail[-room:] if room else ""
                # Keep the newest text, including when characters occupy two cells.
                while cell_len(tail) > room:
                    tail = tail[1:]
                child_lines.append(Text(label + tail, style="dim", no_wrap=True, overflow="crop"))
            if len(children) > MAX_CHILD_LINES:
                child_lines.append(Text(f"+{len(children) - MAX_CHILD_LINES} more subagents",
                                        style="dim", no_wrap=True, overflow="ellipsis"))
            rows = max(1, height - 1 - len(child_lines))
            lines = Text(self._assistant_text).wrap(console, max(1, width))
            if self._assistant_text:
                child_lines.append(Text("\n").join(lines[-rows:]))
            preview = Group(*child_lines)
            if self._assistant_live is None:
                self._assistant_live = Live(preview, console=console, auto_refresh=False, transient=True)
                self._assistant_live.start(refresh=True)
            else:
                self._assistant_live.update(preview, refresh=True)
        else:
            # Allow two terminal cells per printable character, as for parent tails.
            cells = max(0, (width - 1) // 2)
            if self._assistant_text:
                text = _printable(self._assistant_text)[-cells:] if cells else ""
            else:
                child = children[0]
                label = f"[{child.agent} d{child.depth}] "
                suffix = f" (+{len(children) - 1})" if len(children) > 1 else ""
                room = max(0, cells - len(label) - len(suffix))
                text = (label + (child.tail[-room:] if room else "") + suffix)[:cells]
            self._out("\r\033[2K" + text, end="")
            self._plain_visible = True

    def discard_assistant(self) -> None:
        self._assistant_text = ""
        self._children.clear()
        self._draw(force=True)

    @contextlib.contextmanager
    def suspended(self):
        """Close the display, retaining bounded state to redraw when input ends."""
        self._suspended += 1
        if self._suspended == 1:
            if self._status is not None:
                self._status.stop()
            self._close_preview()
        try:
            yield
        finally:
            self._suspended -= 1
            if self._suspended == 0:
                self._draw(force=True)

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
