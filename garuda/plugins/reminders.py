"""Trusted, declarative reminders. No workspace execution or authority changes."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import math
import os
import stat
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

import yaml

from garuda.core.events import EventStore, EventType
from garuda.types import ToolCall, ToolResult

logger = logging.getLogger(__name__)

MAX_RULES = 32
MAX_INJECTIONS = 64
MAX_ARGUMENT_CHARS = 16_384
MAX_COUNT = (1 << 63) - 1
MAX_EVENTS = 256


class ReminderObserver(Protocol):
    def observe_tool(self, call: ToolCall, result: ToolResult) -> None: ...


class NoteQueue(Protocol):
    def queue(self, note: str) -> None: ...


class ReminderConfigError(ValueError):
    pass


def _text(value, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ReminderConfigError(f"expected nonempty text of at most {limit} characters")
    return value


def _keys(value, allowed: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ReminderConfigError("expected a mapping with only documented fields")
    return value


def _integer(value, limit: int) -> int:
    if type(value) is not int or not 1 <= value <= limit:
        raise ReminderConfigError(f"expected an integer from 1 to {limit}")
    return value


@dataclass(frozen=True)
class ReminderRule:
    id: str
    source: str
    text: str
    tool: str | None
    outcome: str | None
    arguments: tuple[tuple[str, str, str], ...]
    max_injections: int


def _rule(entry, source: str) -> ReminderRule:
    entry = _keys(entry, {"id", "text", "when", "max_injections"})
    identity = _text(entry.get("id"), 64)
    if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in identity):
        raise ReminderConfigError("id must contain only ASCII letters, digits, _, . or -")
    when = _keys(entry.get("when"), {"tool", "event", "outcome", "arguments"})
    tool = _text(when["tool"], 256) if "tool" in when else None
    if "event" in when and when["event"] != "tool_result":
        raise ReminderConfigError("the only supported event is tool_result")
    if tool is None and "event" not in when:
        raise ReminderConfigError("when requires tool or event: tool_result")
    outcome = when.get("outcome")
    if "outcome" in when and outcome not in ("success", "error"):
        raise ReminderConfigError("outcome must be success or error")
    conditions = when.get("arguments", {})
    if not isinstance(conditions, dict) or len(conditions) > 8 or (conditions and tool is None):
        raise ReminderConfigError("arguments requires tool and at most 8 named conditions")
    arguments = []
    for key, condition in conditions.items():
        key = _text(key, 128)
        condition = _keys(condition, {"literal", "glob"})
        if len(condition) != 1:
            raise ReminderConfigError("each argument condition requires one literal or glob")
        kind, value = next(iter(condition.items()))
        arguments.append((key, kind, _text(value, 256 if kind == "glob" else 8192)))
    return ReminderRule(identity, source, _text(entry.get("text"), 2048), tool, outcome,
                        tuple(arguments), _integer(entry.get("max_injections", 1), 8))


@dataclass
class ReminderConfig:
    rules: list[ReminderRule] = field(default_factory=list)
    max_injections: int = 8


def load_reminders(workspace=None, *, strict: bool = False) -> ReminderConfig:
    """Use the existing global project-hook trust opt-in, never a project flag.

    Files are read with a byte ceiling; values and trust are parsed from those
    same bytes. A bad rule is reported locally without printing its contents.
    """
    from garuda.agents.frontmatter import load_yaml_unique
    from garuda.config.agent_home import AGENT_HOME_DIRS, global_settings_path
    from garuda.config_sources import record_source, record_source_error

    config = ReminderConfig()

    def problem(source, location, message):
        detail = f"reminder.invalid: {source} {location}: {message}"
        logger.warning(detail)
        if strict:
            raise ReminderConfigError(detail)

    def read(path):
        source = str(path.absolute())
        if len(source) > 1024 or any(ord(c) < 32 for c in source):
            problem("(settings path)", "reminders", "unsupported source path")
            return None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("settings must be a regular file")
                raw = handle.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("settings exceeds 1 MiB")
            record_source(path, raw)
            data = load_yaml_unique(raw.decode("utf-8"))
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise ValueError("settings must be a mapping")
            return data
        except FileNotFoundError:
            record_source(path, None)
            return None
        except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError):
            record_source_error()
            problem(source, "reminders", "cannot read bounded, unique-key YAML settings")
            return None

    global_path = global_settings_path()
    global_data = read(global_path) or {}
    block = global_data.get("reminders")
    if isinstance(block, dict) and block.get("strict") is True:
        strict = True
    project_trusted = global_data.get("trust_project_hooks") is True

    def extend(data, path, *, project=False):
        if "reminders" not in data:
            return
        source = str(path.absolute())
        if project and not project_trusted:
            logger.warning("reminder.untrusted: ignoring project reminders in %s; "
                           "trust_project_hooks: true must be set in global settings", source)
            return
        try:
            block = _keys(data["reminders"], {"rules"} if project else {"rules", "strict", "max_injections"})
            if not project:
                if "strict" in block and type(block["strict"]) is not bool:
                    raise ReminderConfigError("strict must be a boolean")
                config.max_injections = _integer(block.get("max_injections", 8), MAX_INJECTIONS)
            entries = block.get("rules", [])
            if not isinstance(entries, list) or len(entries) > MAX_RULES:
                raise ReminderConfigError(f"rules must be a list of at most {MAX_RULES} entries")
        except ReminderConfigError as exc:
            problem(source, "reminders", str(exc))
            return
        seen = set()
        for index, entry in enumerate(entries):
            try:
                rule = _rule(entry, source)
                if rule.id in seen:
                    config.rules[:] = [prior for prior in config.rules
                                       if (prior.source, prior.id) != (source, rule.id)]
                    raise ReminderConfigError("duplicate rule id in source")
                seen.add(rule.id)
                if len(config.rules) >= MAX_RULES:
                    raise ReminderConfigError(f"at most {MAX_RULES} rules may load across sources")
                config.rules.append(rule)
            except ReminderConfigError as exc:
                problem(source, f"reminders.rules[{index}]", str(exc))

    extend(global_data, global_path)
    if workspace is not None:
        for name in AGENT_HOME_DIRS:
            path = Path(workspace) / name / "settings.yaml"
            data = read(path)
            if data is not None:
                extend(data, path, project=True)
    return config


def _argument_digest(arguments) -> str | None:
    """Validate the complete JSON tree before serialization; never match a prefix."""
    if type(arguments) is not dict:
        return None
    stack = [(arguments, 0)]
    nodes, chars = 0, 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > 256 or depth > 8:
            return None
        if type(value) is str:
            chars += len(value)
        elif type(value) in (dict, list):
            if len(value) > 256 or len(stack) + len(value) * 2 > 512:
                return None
            if type(value) is dict:
                if any(type(key) is not str for key in value):
                    return None
                stack.extend((key, depth + 1) for key in value)
                stack.extend((item, depth + 1) for item in value.values())
            else:
                stack.extend((item, depth + 1) for item in value)
        elif type(value) is float:
            if not math.isfinite(value):
                return None
        elif type(value) is int:
            if value.bit_length() > 64:
                return None
        elif value is not None and type(value) is not bool:
            return None
        if chars > MAX_ARGUMENT_CHARS:
            return None
    raw = json.dumps(arguments, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass
class _Counts:
    triggered: int = 0
    injected: int = 0
    duplicate: int = 0
    rule_cap: int = 0
    run_cap: int = 0


class ReminderRun:
    """One run's budgets survive compaction; only steering.flush delivers text."""

    def __init__(self, config: ReminderConfig, events: EventStore):
        self.config = config
        self.events = events
        self.counts = {rule: _Counts() for rule in config.rules}
        self.pending: dict[ReminderRule, tuple] = {}
        self.seen: set[tuple] = set()
        self.injected = 0
        self.run_id = str(uuid.uuid4())
        self.recorded = 0
        self.omitted = 0
        self.finished = False

    def _record(self, rule, action):
        counts = self.counts[rule]
        count = min(MAX_COUNT, getattr(counts, action) + 1)
        setattr(counts, action, count)
        if self.recorded >= MAX_EVENTS:
            self.omitted = min(MAX_COUNT, self.omitted + 1)
            return
        self.recorded += 1
        self._emit(rule, action, count)

    def _emit(self, rule, action, count):
        self.events.append(EventType.REMINDER, {
            "run_id": self.run_id, "rule_id": rule.id, "source": rule.source,
            "action": action, "count": count, "counts": asdict(self.counts[rule]),
            "events_omitted": self.omitted,
        })

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        for rule, counts in self.counts.items():
            self._emit(rule, "summary", counts.injected)

    def observe_tool(self, call: ToolCall, result: ToolResult) -> None:
        if type(result.is_error) is not bool or not isinstance(call.name, str) or len(call.name) > 256:
            return
        outcome = "error" if result.is_error else "success"
        digest = _argument_digest(call.arguments)
        for rule, counts in self.counts.items():
            if rule.outcome is not None and rule.outcome != outcome:
                continue
            if rule.tool is not None:
                if digest is None or not fnmatch.fnmatchcase(call.name, rule.tool):
                    continue
                if any(not isinstance(call.arguments.get(key), str)
                       or not (call.arguments[key] == value if kind == "literal"
                               else fnmatch.fnmatchcase(call.arguments[key], value))
                       for key, kind, value in rule.arguments):
                    continue
            signature = (rule.source, rule.id, call.name, digest, outcome)
            self._record(rule, "triggered")
            if rule in self.pending or (digest is not None and signature in self.seen):
                self._record(rule, "duplicate")
            elif counts.injected >= rule.max_injections:
                self._record(rule, "rule_cap")
            elif self.injected + len(self.pending) >= self.config.max_injections:
                self._record(rule, "run_cap")
            else:
                self.pending[rule] = signature

    def flush(self, queue: NoteQueue) -> None:
        for rule, signature in self.pending.items():
            self.injected += 1
            self.seen.add(signature)
            self._record(rule, "injected")
            queue.queue(f"[reminder id={rule.id} source={json.dumps(rule.source)} "
                        f"count={self.counts[rule].injected}; advisory]\n{rule.text}")
        self.pending.clear()


def _session_boundary(event, store):
    if event["type"] == EventType.SESSION_START.value:
        store.reminder_run = None
    elif event["type"] == EventType.SESSION_END.value and store.reminder_run is not None:
        store.reminder_run.finish()


def bind_reminders(config: ReminderConfig, events: EventStore) -> ReminderRun:
    """One budget across native phases; the owning session events delimit it."""
    if events.reminder_run is None:
        events.reminder_run = ReminderRun(config, events)
    events.add_observer(_session_boundary)
    return events.reminder_run


def inspect_reminders(events) -> list[dict]:
    """Cumulative source-labelled counts; no records means unknown, not zero."""
    rows = {}
    for event in events:
        if event.get("type") != EventType.REMINDER.value:
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        key = (payload.get("run_id"), payload.get("source"), payload.get("rule_id"))
        counts = payload.get("counts")
        if (not all(isinstance(part, str) for part in key) or not isinstance(counts, dict)
                or set(counts) != set(_Counts.__dataclass_fields__)
                or any(type(count) is not int or not 0 <= count <= MAX_COUNT for count in counts.values())):
            continue
        rows[key] = {"run_id": key[0], "source": key[1], "rule_id": key[2], **counts,
                     "final": payload.get("action") == "summary",
                     "events_omitted": payload.get("events_omitted")}
    return list(rows.values())
