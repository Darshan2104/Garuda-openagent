"""Shared by the check scripts: which browser to launch, and waiting for a condition.

Browser. Playwright's bundled Chromium by default, so a check behaves the same on a laptop and
on a CI runner (`playwright install chromium`). Set ``GARUDA_CHECK_CHANNEL=chrome`` (or pass
``--channel chrome`` to ``run_checks.py``) to drive an installed Google Chrome instead. The
Linux owned-start checks always use Google Chrome; they say so themselves.

Waiting. A fixed sleep before an assertion is a race: too short on a cold or loaded machine,
wasted time everywhere else. :func:`settle` waits for the condition the assertion is about and
returns ``False`` on timeout, so the check that follows still reports what it saw. Sleeps that
remain in the checks are observation windows (counting requests while the page sits still),
not waits.
"""

from __future__ import annotations

import os

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

DEFAULT_TIMEOUT_MS = 15_000
#: A page load may take as long as the runner allows a dashboard to start: on a cold or
#: throttled machine the first load of a busy dashboard can pass Playwright's 30 s default.
NAV_TIMEOUT_MS = float(os.environ.get("GARUDA_CHECK_START_TIMEOUT", "120")) * 1000


def launch(playwright, **kwargs):
    channel = os.environ.get("GARUDA_CHECK_CHANNEL") or None
    return playwright.chromium.launch(channel=channel, **kwargs)


def settle(page, expression: str, arg=None, timeout: float = DEFAULT_TIMEOUT_MS) -> bool:
    """Wait until the JavaScript predicate ``expression`` holds; ``False`` if it never does."""
    try:
        page.wait_for_function(expression, arg=arg, timeout=timeout)
    except PlaywrightTimeoutError:
        return False
    return True


def count_at_least(page, selector: str, n: int = 1, timeout: float = DEFAULT_TIMEOUT_MS) -> bool:
    return settle(page, "([s, n]) => document.querySelectorAll(s).length >= n", [selector, n],
                  timeout)


def count_is(page, selector: str, n: int, timeout: float = DEFAULT_TIMEOUT_MS) -> bool:
    return settle(page, "([s, n]) => document.querySelectorAll(s).length === n", [selector, n],
                  timeout)


def text_contains(page, selector: str, needle: str, timeout: float = DEFAULT_TIMEOUT_MS) -> bool:
    return settle(page, """([s, needle]) => {
        const node = document.querySelector(s);
        return !!node && node.innerText.includes(needle);
    }""", [selector, needle], timeout)
