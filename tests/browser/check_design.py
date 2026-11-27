"""The design system's invariants, on every page: the regressions a restyle is likely to bring.

Run by ``run_checks.py`` against a read-only dashboard over the overview and observability
fixtures (``GARUDA_CHECK_BASE``) and, for the run traces, the inspector's dashboard
(``GARUDA_CHECK_RO_BASE``). On every route it checks:

* **no sideways scroll on a phone**: at 390 px the page is never wider than the window;
* **no unstyled state pill**: every visible ``.pill`` has a fill or an outline and a colour of
  its own (a pill whose state rule went missing renders as plain body text);
* **chips stay on one line**: every ``.chip[data-chip]`` reads ``key: value`` in ``innerText``
  without a line break (a flex chip once split ``verification: unavailable``);
* **a visible focus ring**: every element Tab reaches shows an outline (or, for a field, its
  focus halo).

And the paused flow (``overview-flow-deadline``, stopped on a resumable deadline): its header
says "paused", its stopped step is drawn paused by a data attribute (not ``:has()``, which
older Firefox lacks), never as an ordinary failure. Fails on any console error.
"""

import os
import sys
import tempfile
from pathlib import Path

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8897/")
RO_BASE = os.environ.get("GARUDA_CHECK_RO_BASE")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))

SID = "#/sessions/00000000-0000-0000-0000-0000000000"
ROUTES = ["#/", "#/starters", "#/runs", "#/sessions", SID + "03", SID + "05", SID + "08",
          SID + "04", SID + "b5", SID + "e2", SID + "e3", "#/inbox", "#/usage", "#/setup",
          "#/providers", "#/runtimes", "#/chat", "#/nope"]
TRACES = ["#/runs/b2-rejected-then-passed", "#/runs/j10-external-acp", "#/runs/l12-flow-completed",
          "#/runs/m13-flow-stopped", "#/runs/n14-flow-running"]
PAUSED = SID + "e2"

LOADED = """() => {
  const view = document.querySelector('#view');
  return !!view && view.children.length > 0 && !view.querySelector('.loading')
    && !document.querySelector('#view [aria-busy="true"]:not(.btn)');
}"""

INSPECT = """() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
    && getComputedStyle(el).visibility !== 'hidden';
  const alpha = c => { const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return 1;
    const p = m[1].split(/[ ,/]+/).filter(Boolean); return p.length > 3 ? parseFloat(p[3]) : 1; };
  const name = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
    (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).join('.') : '');
  const body = getComputedStyle(document.body).color;
  const pills = [], chips = [];
  document.querySelectorAll('#view .pill, .topbar .pill').forEach(el => {
    if (!visible(el)) return;
    const cs = getComputedStyle(el);
    const fill = alpha(cs.backgroundColor) > 0;
    const outline = parseFloat(cs.borderTopWidth) > 0 && alpha(cs.borderTopColor) > 0;
    if (!(fill || outline) || cs.color === body) pills.push(name(el) + ' "' + el.textContent.trim() + '"');
  });
  document.querySelectorAll('#view .chip[data-chip]').forEach(el => {
    if (visible(el) && el.innerText.includes('\\n')) chips.push(JSON.stringify(el.innerText));
  });
  // What sticks out past the window without a scrolling box to hold it (for the report):
  // an element, or a run of text wider than its box (a long path with no break).
  const wide = [];
  const held = el => {
    for (let p = el; p; p = p.parentElement) {
      if (['auto', 'scroll', 'hidden'].includes(getComputedStyle(p).overflowX)
          && p.getBoundingClientRect().right <= window.innerWidth + 1) return true;
    }
    return false;
  };
  if (document.documentElement.scrollWidth > window.innerWidth) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node && wide.length < 3; node = walker.nextNode()) {
      const box = node.nodeType === 3 ? (() => { const r = document.createRange(); r.selectNodeContents(node); return r; })() : node;
      const owner = node.nodeType === 3 ? node.parentElement : node;
      if (box.getBoundingClientRect().right > window.innerWidth + 1 && !held(owner.parentElement)) {
        wide.push(name(owner) + (node.nodeType === 3 ? ' text "' + node.textContent.slice(0, 40) + '"' : ''));
      }
    }
  }
  return {
    width: document.documentElement.scrollWidth, window: window.innerWidth, pills, chips, wide,
  };
}"""

FOCUSED = """() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const cs = getComputedStyle(el);
  const ring = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 1;
  const halo = cs.boxShadow && cs.boxShadow !== 'none';
  return { ok: ring || halo, what: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
           (el.getAttribute('href') ? '[href=' + el.getAttribute('href') + ']' : '') };
}"""


def main() -> int:
    from browser_support import launch as launch_browser
    from browser_support import settle, throttle
    from playwright.sync_api import sync_playwright

    problems: list[str] = []

    def check(label, cond, detail=""):
        print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
        if not cond:
            problems.append(f"{label} {detail}".strip())

    routes = [(BASE, r) for r in ROUTES] + [(RO_BASE, r) for r in TRACES if RO_BASE]
    with sync_playwright() as p:
        browser = launch_browser(p)
        for width, height in ((390, 844), (1440, 900)):
            context = browser.new_context(viewport={"width": width, "height": height})
            page = throttle(context.new_page())
            page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
            for base in {b for b, _ in routes}:
                page.goto(f"{base}#t={TOKEN}", wait_until="load")
                settle(page, LOADED)
            for base, route in routes:
                page.goto(f"{base}{route}", wait_until="load")
                loaded = settle(page, LOADED)
                found = page.evaluate(INSPECT)
                where = f"{route} at {width}px"
                check(f"{where}: loaded", loaded)
                if width == 390:
                    check(f"{where}: no sideways page scroll", found["width"] <= found["window"],
                          f"(page {found['width']}px wide: {found['wide']})")
                check(f"{where}: every state pill is styled", not found["pills"], str(found["pills"][:3]))
                check(f"{where}: chips keep key: value on one line", not found["chips"],
                      str(found["chips"][:3]))
                if width == 1440:
                    # Tab through the page from the top: everything focusable shows a ring.
                    page.evaluate("document.activeElement && document.activeElement.blur()")
                    page.evaluate("window.scrollTo(0, 0)")
                    missing, seen = [], set()
                    for _ in range(40):
                        page.keyboard.press("Tab")
                        state = page.evaluate(FOCUSED)
                        if state is None or state["what"] in seen:
                            continue
                        seen.add(state["what"])
                        if not state["ok"]:
                            missing.append(state["what"])
                    check(f"{where}: every focused element shows a focus ring", not missing,
                          str(missing[:4]))
                    page.keyboard.press("Escape")
            if width == 390:
                page.goto(f"{BASE}#/sessions", wait_until="load")
                settle(page, LOADED)
                page.click("#nav-menu")
                found = page.evaluate(INSPECT)
                check("the phone menu, open, adds no sideways scroll", found["width"] <= found["window"])
                page.screenshot(path=str(SHOTS / "design-phone-menu.png"))
                page.keyboard.press("Escape")

            # --- the paused flow: paused everywhere it is drawn, by attribute -----------------
            page.goto(f"{BASE}{PAUSED}", wait_until="load")
            settle(page, "() => !!document.querySelector('li.pipe-step[data-step=\"code\"]')")
            head = page.evaluate("""() => {
              const pill = document.querySelector('#session-label');
              const step = document.querySelector('li.pipe-step[data-step="code"]');
              const stepPill = step && step.querySelector('.pipe-head > .pill');
              const marker = el => el ? getComputedStyle(el, '::before').backgroundImage : '';
              return { text: pill && pill.textContent, state: pill && pill.dataset.state,
                       marker: marker(pill), reason: (document.querySelector('#session-pause') || {}).textContent,
                       pause: step && step.dataset.pause, stepCls: stepPill && stepPill.className,
                       stepMarker: marker(stepPill),
                       cause: (document.querySelector('#session-cause') || {dataset: {}}).dataset.state };
            }""")
            check(f"paused flow at {width}px: the header says paused", head["text"] == "paused"
                  and head["state"] == "paused", str(head))
            check(f"paused flow at {width}px: with its reason", "deadline" in (head["reason"] or ""), str(head))
            check(f"paused flow at {width}px: the stopped step is marked paused by attribute",
                  head["pause"] == "deadline" and "paused" in (head["stepCls"] or "")
                  and "failed" not in (head["stepCls"] or ""), str(head))
            check(f"paused flow at {width}px: pause markers are the two bars, not a failure dot",
                  "gradient" in head["marker"] and "gradient" in head["stepMarker"], str(head))
            check(f"paused flow at {width}px: the Result's cause is drawn as a pause", head["cause"] == "paused")
            page.screenshot(path=str(SHOTS / f"design-paused-{width}.png"), full_page=True)
            context.close()
        browser.close()

    print("\n=== problems ===")
    for problem in problems:
        print("  " + problem)
    if not problems:
        print("  none")
        print("\nOK: no sideways scroll on a phone, every pill styled, chips on one line, focus "
              "rings everywhere, and a paused flow drawn as paused.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
