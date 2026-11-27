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

With ``GARUDA_CHECK_SNAPSHOTS=1`` it also screenshots every route and fixture state (the
paused/replaced flows, the step in flight, approvals, chat, Flows, traces with every turn open,
the phone menu, the keyboard help, the connection banners, the token page, an empty store) at
1440x900 and 390x844 in light and dark, animations off, into ``<shots>/snap/``. Under
``GARUDA_CHECK_CSS_AUDIT`` (``css_audit.py``) each of those is also compared pixel for pixel
against a baseline stylesheet; that is how a CSS-only refactor proves it changed nothing.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8897/")
RO_BASE = os.environ.get("GARUDA_CHECK_RO_BASE")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))

SID = "#/sessions/00000000-0000-0000-0000-0000000000"
ROUTES = ["#/", "#/flows", "#/flows/plan-only", "#/starters", "#/runs", "#/sessions",
          SID + "03", SID + "05", SID + "08",
          SID + "04", SID + "b5", SID + "e2", SID + "e3", "#/inbox", "#/usage", "#/setup",
          "#/providers", "#/runtimes", "#/chat", "#/nope"]
TRACES = ["#/runs/b2-rejected-then-passed", "#/runs/j10-external-acp", "#/runs/l12-flow-completed",
          "#/runs/m13-flow-stopped", "#/runs/n14-flow-running"]
PAUSED = SID + "e2"
#: The live-step fixtures (check_live_step.py): a flow with a step in flight, a paused flow, a
#: measured finished flow, and the Overview and Sessions list over them (nested step rows,
#: folded attention rows, the step-session line), on their own dashboard.
LIVE_BASE = os.environ.get("GARUDA_CHECK_LIVE_BASE")
LIVE = "#/sessions/00000000-0000-0000-0000-00000000f00"
LIVE_ROUTES = [LIVE + "1", LIVE + "3", LIVE + "4", LIVE + "9", "#/sessions/00000000-0000-0000-0000-00000000f00d",
               "#/runs/00000000-0000-0000-0000-00000000f001", "#/", "#/sessions"]

SNAPSHOTS = bool(os.environ.get("GARUDA_CHECK_SNAPSHOTS"))
#: Extra dashboards the snapshot walk covers when run_checks.py started them.
FLOWS_BASE = os.environ.get("GARUDA_CHECK_FLOWS_BASE")
EMPTY_BASE = os.environ.get("GARUDA_CHECK_EMPTY_BASE")
CHAT_BASE = os.environ.get("GARUDA_CHECK_CHAT_BASE")
MORE_TRACES = ["#/runs/a1-approved-gate", "#/runs/c3-context-pressure", "#/runs/d4-killed-midrun",
               "#/runs/e5-rigorous-phases", "#/runs/f6-old-format", "#/runs/g7-truncated",
               "#/runs/h8-subagent", "#/runs/k11-long-log", "#/runs/no-such-run"]

LOADED = """() => {
  const view = document.querySelector('#view');
  return !!view && view.children.length > 0 && !view.querySelector('.loading')
    && !document.querySelector('#view [aria-busy="true"]:not(.btn)');
}"""

#: What may scroll sideways inside its card on a phone: code, diffs, raw payloads, charts.
INNER_SCROLL_ALLOWED = ("pre, code, textarea, .diff, .diff-body, .raw-payload, .starter-data, "
                        ".final-text, .task-full, svg, .chart, figure")

INSPECT = """() => {
  const INNER_SCROLL_ALLOWED = '__ALLOWED__';

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
  // Boxes that scroll sideways inside the page: a table that should stack, a list that should
  // wrap. Only genuinely wide content (code, diffs, raw payloads, charts) may scroll.
  const inner = [];
  document.querySelectorAll('#view *').forEach(el => {
    if (inner.length >= 5 || !visible(el)) return;
    const cs = getComputedStyle(el);
    if (!['auto', 'scroll'].includes(cs.overflowX) || el.scrollWidth <= el.clientWidth + 1) return;
    if (el.closest(INNER_SCROLL_ALLOWED)) return;
    inner.push(name(el) + ' (' + el.scrollWidth + ' > ' + el.clientWidth + ')');
  });
  return {
    width: document.documentElement.scrollWidth, window: window.innerWidth, pills, chips, wide, inner,
  };
}"""
INSPECT = INSPECT.replace("__ALLOWED__", INNER_SCROLL_ALLOWED)

FOCUSED = """() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const cs = getComputedStyle(el);
  const ring = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 1;
  const halo = cs.boxShadow && cs.boxShadow !== 'none';
  return { ok: ring || halo, what: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
           (el.getAttribute('href') ? '[href=' + el.getAttribute('href') + ']' : '') };
}"""


def snapshot_walk(browser, check) -> None:
    """Every route and fixture state, both sizes, both themes, into ``<shots>/snap/``."""
    from browser_support import settle

    out = SHOTS / "snap"
    out.mkdir(exist_ok=True)
    walk = ([("main", BASE, r) for r in ROUTES]
            + [("ro", RO_BASE, r) for r in TRACES + MORE_TRACES + ["#/runs"] if RO_BASE]
            + [("live", LIVE_BASE, r) for r in LIVE_ROUTES if LIVE_BASE]
            + [("flows", FLOWS_BASE, r) for r in ("#/flows", "#/flows/two-step", "#/flows/review-loop",
                                                  "#/flows/fan-out") if FLOWS_BASE]
            + [("empty", EMPTY_BASE, r) for r in ("#/", "#/sessions", "#/runs", "#/inbox", "#/flows",
                                                  "#/usage") if EMPTY_BASE]
            + [("chat", CHAT_BASE, "#/chat")] * bool(CHAT_BASE))

    def name(*parts):
        return "-".join(re.sub(r"[^\w]+", "_", str(p)).strip("_") or "root" for p in parts)

    for width, height in ((1440, 900), (390, 844)):
        for scheme in ("light", "dark"):
            context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
            page = context.new_page()

            def shot(*label, page=page, width=width, scheme=scheme):
                page.evaluate("window.scrollTo(0, 0)")
                tall = page.evaluate("document.documentElement.scrollHeight")
                clip = {"x": 0, "y": 0, "width": width, "height": 12000} if tall > 12000 else None
                page.screenshot(path=str(out / (name(*label, width, scheme) + ".png")), full_page=True,
                                clip=clip)  # the long log's first 12,000 px: a capture has a limit

            for base in {b for _, b, _ in walk}:
                page.goto(f"{base}#t={TOKEN}", wait_until="load")
                settle(page, LOADED)
            for tag, base, route in walk:
                page.goto(f"{base}{route}", wait_until="load")
                check(f"snapshot {tag} {route} {width} {scheme}: loaded", settle(page, LOADED))
                shot(tag, route)
                if page.locator("#expand-all").count():  # a trace with every turn open
                    page.click("#expand-all")
                    settle(page, "() => !document.querySelector('.turn:not(.open)')")
                    if page.locator("[data-subagent]").count():
                        page.locator("[data-subagent]").first.click()
                        settle(page, "() => !!document.querySelector('.sub-turn')")
                    shot(tag, route, "open")
                if tag == "flows" and page.locator("#flow-preview-button").count():
                    page.locator('[data-field="goal"]').fill("Fix the failing parser test.")
                    page.locator("#flow-preview-button").click()
                    settle(page, "() => !!document.querySelector('#flow-command')", timeout=20_000)
                    shot(tag, route, "preview")
            # The app's own states: keyboard help, the phone menu, banners, the token page.
            page.goto(f"{BASE}#/runs", wait_until="load")
            settle(page, "() => !!document.querySelector('#runs-table')")
            page.locator("body").press("?")
            settle(page, "() => !document.getElementById('keys-help').hidden")
            shot("keys-help")
            page.keyboard.press("Escape")
            if width == 390:
                page.click("#nav-menu")
                shot("phone-menu")
                page.keyboard.press("Escape")
            for reason, refuse in (("down", lambda route: route.abort("connectionrefused")),
                                   ("busy", lambda route: route.fulfill(status=503, body=""))):
                page.route("**/api/**", refuse)
                page.click("#runs-refresh")
                settle(page, "() => !document.getElementById('conn-banner').hidden")
                shot("banner", reason)
                page.unroute("**/api/**")
                page.click("#conn-retry")
                settle(page, "() => document.getElementById('conn-banner').hidden", timeout=20_000)
            page.evaluate("sessionStorage.setItem('garuda_token', 'stale-token-0000')")
            page.reload(wait_until="load")
            settle(page, "() => !!document.getElementById('token-panel')")
            shot("token")
            context.close()


def main() -> int:
    from browser_support import launch as launch_browser
    from browser_support import settle, throttle
    from playwright.sync_api import sync_playwright

    problems: list[str] = []

    def check(label, cond, detail=""):
        print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
        if not cond:
            problems.append(f"{label} {detail}".strip())

    routes = ([(BASE, r) for r in ROUTES] + [(RO_BASE, r) for r in TRACES if RO_BASE]
              + [(LIVE_BASE, r) for r in LIVE_ROUTES if LIVE_BASE])
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
                    check(f"{where}: nothing scrolls sideways inside the page", not found["inner"],
                          str(found["inner"]))
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

                # --- the Sessions list on a phone: a page of 50 compact cards ----------------
                for scheme in ("light", "dark"):
                    page.emulate_media(color_scheme=scheme)
                    page.goto(f"{BASE}#/overview", wait_until="load")
                    page.goto(f"{BASE}#/sessions", wait_until="load")
                    settle(page, "() => document.querySelectorAll('tr.session-row').length >= 50")
                    cards = page.evaluate("""() => {
                      const rows = [...document.querySelectorAll('tr.session-row')];
                      const table = document.querySelector('#sessions-table');
                      return { count: rows.length, height: table ? table.getBoundingClientRect().height : 0,
                               tallest: Math.max(...rows.map(r => r.getBoundingClientRect().height)),
                               sideways: rows.filter(r => r.scrollWidth > r.clientWidth + 1).length,
                               details: rows.filter(r => r.querySelector('td.s-more details')).length,
                               hidden: rows.filter(r => getComputedStyle(r.querySelector('td[data-th="Workspace"]')).display !== 'none').length };
                    }""")
                    check(f"390px Sessions ({scheme}): 50 cards stay under 9,000 px, each compact",
                          cards["count"] >= 50 and cards["height"] < 9000 and cards["tallest"] <= 200, str(cards))
                    check(f"390px Sessions ({scheme}): no card scrolls sideways; the rest is behind details",
                          cards["sideways"] == 0 and cards["details"] == cards["count"] and cards["hidden"] == 0, str(cards))
                    page.screenshot(path=str(SHOTS / f"design-sessions-390-{scheme}.png"), full_page=True)
                    page.screenshot(path=str(SHOTS / f"design-sessions-390-{scheme}-top.png"))
                    page.click("tr.session-row td.s-more summary")
                    opened = page.evaluate("""() => { const td = document.querySelector('tr.session-row td.s-more');
                      const row = td.closest('tr'); return { text: td.innerText, wide: td.getBoundingClientRect().width,
                      card: row.getBoundingClientRect().width, sideways: row.scrollWidth > row.clientWidth + 1 }; }""")
                    check(f"390px Sessions ({scheme}): details opens across the card with the workspace and start",
                          "Workspace" in opened["text"] and "Started" in opened["text"]
                          and opened["wide"] > opened["card"] * 0.8 and not opened["sideways"], str(opened))
                page.emulate_media(color_scheme="light")

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
        if SNAPSHOTS:
            snapshot_walk(browser, check)
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
