"""Bounded requests for owned checks and role execution.

The caller chooses a trusted execution purpose. Both this selector and the
private worker enforce its limits; command text and environment cannot widen it.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

WIRE_VERSION = 2
#: A role worker may run as long as the longest step deadline (`limits.deadline_min`, 7 days)
#: plus the owned backstop margin (`garuda.runtime.owned_role.DEADLINE_MARGIN`), so a checked
#: step with a deadline past 50 minutes (#828) or a `long_run` step can start at all.
ROLE_TIMEOUT_CAP = 7 * 24 * 3600 + 600
#: A check command may be configured up to 4 h (``check_limits``).
REQUEST_LIMITS = {"acceptance": (4 * 3600, 65_536), "role": (ROLE_TIMEOUT_CAP, 2_097_152)}
MAX_REQUEST_BYTES = max(value[1] for value in REQUEST_LIMITS.values())


@dataclass(frozen=True)
class ProcessRequest:
    run: str | tuple[str, ...]
    cwd: str
    env: tuple[tuple[str, str], ...] = ()
    timeout: float = 900.0
    term_grace: float = 3.0
    kill_grace: float = 3.0
    input_data: str | None = None
    purpose: str = "acceptance"

    def document(self) -> dict:
        if not isinstance(self.run, (str, tuple)) or not self.run:
            raise ValueError("command must be nonempty shell text or an argv tuple")
        if isinstance(self.run, tuple) and (not self.run[0] or any(not isinstance(v, str) for v in self.run)):
            raise ValueError("argv must contain strings and a nonempty executable")
        if not isinstance(self.cwd, str) or not os.path.isabs(self.cwd):
            raise ValueError("cwd must be absolute")
        if self.purpose not in REQUEST_LIMITS:
            raise ValueError("unknown owned process purpose")
        timeout_cap, byte_cap = REQUEST_LIMITS[self.purpose]
        for value, maximum in ((self.timeout, timeout_cap), (self.term_grace, 3), (self.kill_grace, 3)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
                    math.isfinite(value) and 0 < value <= maximum):
                raise ValueError("process limits must be positive and within policy bounds")
        env = dict(self.env)
        if len(env) != len(self.env) or any(not isinstance(k, str) or not k or "=" in k
                                         or not isinstance(v, str) for k, v in self.env):
            raise ValueError("invalid environment definitions")
        if self.input_data is not None and not isinstance(self.input_data, str):
            raise ValueError("owned helper input must be text")
        result = {"version": WIRE_VERSION, "purpose": self.purpose, "run": self.run, "cwd": self.cwd, "env": env,
                  "timeout": self.timeout, "term_grace": self.term_grace,
                  "kill_grace": self.kill_grace, "input_data": self.input_data}
        if len(json.dumps(result).encode()) > byte_cap - 1:
            raise ValueError("process request exceeds its bound")
        return result
