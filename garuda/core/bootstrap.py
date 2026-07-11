"""Environment bootstrap snapshot.

A fresh agent normally burns its first 2-5 turns rediscovering the same facts on
every task: what OS this is, which language runtimes and package managers exist,
what's in the working directory, whether it's a git repo. Each of those turns is a
full model round-trip — the single biggest avoidable cost/latency sink in the loop,
and the top Terminal-Bench failure mode is still "executable not found".

So we probe the environment *once* at session start with one bounded, read-only
shell command and fold the result into the first-turn system prompt. This is
benchmark-agnostic: it just tells the agent what it's working with, so it can skip
redundant discovery and get to the task. Best-effort throughout — a probe that
fails or times out simply yields no snapshot, never an error.

The result is cached on the ``env`` instance so heterogeneous multi-phase runs
(e.g. rigorous plan + execute, which seed separate fresh contexts over the same
workspace) probe at most once.
"""

from __future__ import annotations

import logging

from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)

# One command, not N: a single round-trip keeps latency flat across local/docker/
# remote (where each exec is expensive). POSIX-sh only (no bashisms) so it runs
# under sh, bash, and container default shells alike. Every probe is read-only and
# self-silencing; unknown tools just produce no line.
_PROBE_SCRIPT = r"""
echo "## OS"
uname -srm 2>/dev/null
if [ -r /etc/os-release ]; then grep -E '^(PRETTY_NAME|VERSION)=' /etc/os-release 2>/dev/null | head -2; fi
sw_vers 2>/dev/null | head -2
echo "## CWD"
pwd 2>/dev/null
ls -A 2>/dev/null | head -40
echo "## Runtimes"
for c in python3 python node deno bun go rustc cargo java gcc make; do
  if command -v "$c" >/dev/null 2>&1; then printf '%s: ' "$c"; "$c" --version 2>&1 | head -1; fi
done
echo "## Package managers"
for c in pip pip3 uv poetry npm pnpm yarn bundle apt-get brew; do
  if command -v "$c" >/dev/null 2>&1; then echo "$c"; fi
done
echo "## Project markers"
ls -A 2>/dev/null | grep -iE '^(package\.json|pyproject\.toml|requirements([._-].*)?\.txt|setup\.(py|cfg)|Cargo\.toml|go\.mod|Makefile|Gemfile|pom\.xml|build\.gradle.*|tsconfig\.json|\.python-version|\.nvmrc|Dockerfile|README([._-].*)?)$' | head -20
echo "## Git"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  printf 'branch: '; git branch --show-current 2>/dev/null
  printf 'head: '; git log --oneline -1 2>/dev/null
else
  echo "not a git repository"
fi
"""

_PROBE_TIMEOUT = 20.0
_MAX_SNAPSHOT_CHARS = 4000
_CACHE_ATTR = "_garuda_env_snapshot"

_HEADER = (
    "## Environment snapshot (auto-detected at session start)\n"
    "This is a one-time probe of your working environment so you can skip redundant "
    "discovery. Trust it for orientation; re-check a specific detail with a tool only "
    "when correctness depends on it.\n"
)


async def environment_snapshot(env: Environment) -> str:
    """Return a compact, prompt-ready environment snapshot for ``env``, or ``""``.

    Cached on the ``env`` instance after the first call so repeated fresh-context
    runs over one workspace probe only once. Never raises.
    """
    cached = getattr(env, _CACHE_ATTR, None)
    if cached is not None:
        return cached

    snapshot = ""
    try:
        result = await env.execute(_PROBE_SCRIPT, timeout=_PROBE_TIMEOUT)
        body = (result.stdout or "").strip()
        if body:
            if len(body) > _MAX_SNAPSHOT_CHARS:
                body = body[:_MAX_SNAPSHOT_CHARS] + "\n… (snapshot truncated)"
            snapshot = f"{_HEADER}\n{body}"
    except Exception:
        logger.debug("Environment probe failed; continuing without a snapshot", exc_info=True)
        snapshot = ""

    try:
        setattr(env, _CACHE_ATTR, snapshot)
    except Exception:
        # Some Environment impls may not accept arbitrary attributes; caching is an
        # optimization, not a correctness requirement.
        pass
    return snapshot
