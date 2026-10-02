"""Consult limits: user ceilings that a project may only lower (plan task G.2, #170)."""

from __future__ import annotations

from dataclasses import dataclass

#: The v1 defaults; the user's ``consults:`` section may change any of them, and a project's
#: may only lower them (the resolver keeps the smaller value). 100 is a hard cap.
DEFAULTS = {
    "max_per_session": 5, "timeout_sec": 600, "max_question_chars": 4000,
    "max_brief_tokens": 2048, "max_answer_chars": 8000, "max_turns": 4,
    "max_output_tokens": 2048,
}
HARD_CAP_PER_ROOT = 100
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Limits:
    max_per_session: int = DEFAULTS["max_per_session"]
    timeout_sec: int = DEFAULTS["timeout_sec"]
    max_question_chars: int = DEFAULTS["max_question_chars"]
    max_brief_tokens: int = DEFAULTS["max_brief_tokens"]
    max_answer_chars: int = DEFAULTS["max_answer_chars"]
    max_turns: int = DEFAULTS["max_turns"]
    max_output_tokens: int = DEFAULTS["max_output_tokens"]

    @property
    def max_brief_chars(self) -> int:
        return self.max_brief_tokens * CHARS_PER_TOKEN

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in DEFAULTS}


def from_config(config: dict | None) -> Limits:
    """The effective limits of a resolved ``garuda.yaml`` (defaults where it is silent),
    never above the hard cap per root task."""
    given = {k: v for k, v in ((config or {}).get("consults") or {}).items() if k in DEFAULTS}
    merged = {**DEFAULTS, **given}
    merged["max_per_session"] = min(int(merged["max_per_session"]), HARD_CAP_PER_ROOT)
    return Limits(**{k: int(v) for k, v in merged.items()})
