"""Stylesheet audit hooks for the browser checks: coverage, redundancy and pixel comparison.

Off unless ``GARUDA_CHECK_CSS_AUDIT`` names a directory; ``browser_support.launch`` then
instruments every browser the checks open, so a whole ``run_checks.py`` walk is recorded
(``css_report.py`` turns the results into a report):

* **rule coverage**: Chromium's CSS rule-usage tracking (DevTools protocol) on every page,
  and every selector of app.css (pointer/focus states made always-true, pseudo-elements
  dropped) that ``querySelector`` ever matched, into ``coverage-<check>-<pid>.json``.
* **redundant declarations** (with ``GARUDA_CHECK_CSS_REDUNDANT=1``, slow): after every
  screenshot, each declaration of every rule that applies is taken out of the live stylesheet
  in turn and the computed values it sets are compared on the elements the rule matches. One
  that never changed anything (overridden, or equal to what applies anyway) is reported. Only
  the states and widths the walk reaches count: read the code before removing one.
* **pixel comparison** (with ``GARUDA_CHECK_CSS_BASELINE=<a saved app.css>``): every
  ``page.screenshot`` is taken twice on the same DOM, once with the baseline stylesheet swapped
  in and once with the served one, animations off. A difference is recorded in
  ``pixels-<check>-<pid>.jsonl`` and both images are kept under ``pixels/``.
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import re
import sys
from pathlib import Path

OUT = Path(os.environ.get("GARUDA_CHECK_CSS_AUDIT") or ".")
BASELINE = os.environ.get("GARUDA_CHECK_CSS_BASELINE")
REDUNDANT = bool(os.environ.get("GARUDA_CHECK_CSS_REDUNDANT"))
TAG = Path(sys.argv[0]).stem
SHEET = "/static/app.css"
MARK = "garuda-baseline"
CSS_FILE = Path(__file__).resolve().parents[2] / "garuda/interfaces/web/static/app.css"
#: Before a selector is tried against the DOM its pseudo-elements are dropped (the element
#: they decorate is what has to exist)...
ELEMENTS = re.compile(r"::?(?:-webkit-|-moz-)[\w-]+|::?(?:before|after|placeholder|selection|marker)\b")
#: ...and pointer/focus states become an always-true pseudo-class, so `:is(:hover, x)` stays valid.
STATES = re.compile(r":(?:hover|focus-visible|focus-within|focus|active)\b")

_used: set[tuple[int, int]] = set()  # rule ranges Chromium marked used
_matched: set[str] = set()  # selectors that matched the DOM
_tested: set[int] = set()  # offsets of declarations taken out at least once
_needed: set[int] = set()  # ...whose removal changed a computed value
_shots = itertools.count(1)


def split_list(head: str) -> list[str]:
    """A selector list split at its top-level commas."""
    parts, depth, cur = [], 0, ""
    for ch in head:
        depth += 1 if ch in "([" else -1 if ch in ")]" else 0
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return [p for p in [*parts, cur.strip()] if p]


def style_rules(text: str) -> list[dict]:
    """Every style rule outside @keyframes: its media, selector, offsets and declarations."""
    text = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group()), text, flags=re.S)  # offsets kept
    out, stack, i, start = [], [], 0, 0
    while i < len(text):
        if text[i] == "{":
            raw = text[start:i]
            stack.append((raw.strip(), start + len(raw) - len(raw.lstrip())))
            start = i + 1
        elif text[i] == "}":
            head, at = stack.pop()
            if head and not head.startswith("@") and not any("keyframes" in h for h, _ in stack):
                decls, pos = [], start
                for piece in re.split(r"(;)", text[start:i]):
                    if piece != ";" and ":" in piece:
                        decls.append((pos + len(piece) - len(piece.lstrip()), piece.strip()))
                    pos += len(piece)
                out.append({"media": " ".join(h for h, _ in stack), "head": head, "start": at,
                            "end": i + 1, "decls": decls})
            start = i + 1
        i += 1
    return out


def probe(selector: str) -> str:
    return STATES.sub(":not(#css-audit-none)", ELEMENTS.sub("", selector)).strip() or "*"


def _plan(text: str) -> list:
    """[normalised selector, element selector, pseudo, body, [[offset, property, body without it]]]."""
    plan = []
    for rule in style_rules(text):
        head = rule["head"]
        if STATES.search(head) or re.search(r"::?(?:-webkit-|-moz-|placeholder|selection|marker)", head):
            continue  # pointer/focus states and odd pseudo-elements are not on a screenshot
        pseudo = set(re.findall(r"::?(before|after)\b", head))
        if len(pseudo) > 1 or (pseudo and any(not re.search(r"::?(before|after)\b", p) for p in split_list(head))):
            continue  # a list mixing elements and pseudo-elements
        tests = [[off, decl.split(":", 1)[0].strip(), "; ".join(d for o, d in rule["decls"] if o != off)]
                 for off, decl in rule["decls"]]
        # Restored from the source text: CSSOM's cssText drops a longhand that came from a
        # shorthand with var() once another longhand overrides part of it.
        whole = "; ".join(d for _, d in rule["decls"])
        plan.append([re.sub(r"[\s\"']", "", head), ELEMENTS.sub("", head),
                     "::" + pseudo.pop() if pseudo else "", whole, tests])
    return plan


_TEXT = CSS_FILE.read_text() if CSS_FILE.exists() else ""
_PROBES = {p: probe(p) for rule in style_rules(_TEXT) for p in split_list(rule["head"])}
_PLAN = _plan(_TEXT) if REDUNDANT else []

MATCH = """(probes) => probes.filter(([_, q]) => {
  try { return document.querySelector(q) !== null; } catch (e) { return false; }
}).map(([s]) => s)"""

#: Swap the stylesheet without an unstyled moment (which would start transitions): load the
#: other sheet beside the current one, then drop the current one.
SWAP = """async (href) => {
  const links = [...document.querySelectorAll('link[rel="stylesheet"][href^="/static/app.css"]')];
  if (!links.length) return false;
  if (links[0].getAttribute('href') !== href) {
    const next = document.createElement('link');
    next.rel = 'stylesheet';
    await new Promise(done => {
      next.addEventListener('load', done, { once: true });
      next.addEventListener('error', done, { once: true });
      next.href = href;
      links[links.length - 1].after(next);
    });
    links.forEach(link => link.remove());
  }
  await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
  return true;
}"""

REDUNDANT_JS = """(plan) => {
  const sheet = [...document.styleSheets].find(s => s.href && new URL(s.href).pathname === '/static/app.css'
                                                   && !s.href.includes('garuda-baseline'));
  if (!sheet) return [[], []];
  const flat = [];
  const walk = list => { for (const r of list) {
    if (r instanceof CSSStyleRule) flat.push(r);
    else if (r.cssRules && !(r instanceof CSSKeyframesRule)) walk(r.cssRules);
  } };
  walk(sheet.cssRules);
  // A transition or an animation would hide a change (the computed value starts where it was).
  const still = sheet.insertRule('*, *::before, *::after { transition: none !important; animation: none !important; }',
                                 sheet.cssRules.length);
  const norm = t => t.replace(/[\\s"']/g, '');
  const longhands = prop => {
    if (prop.startsWith('--')) return [prop];
    const probe = document.createElement('div').style;
    probe.setProperty(prop, 'initial');
    return probe.length ? [...probe] : [prop];
  };
  const tested = [], needed = [];
  let next = 0;
  for (const [key, target, pseudo, whole, decls] of plan) {
    let at = next;
    while (at < flat.length && norm(flat[at].selectorText) !== key) at++;
    if (at >= flat.length) continue;  // dropped by this browser's parser (::-moz-*)
    next = at + 1;
    const rule = flat[at];
    let active = true;
    for (let p = rule.parentRule; p; p = p.parentRule) {
      if (p.media && !matchMedia(p.media.mediaText).matches) active = false;
    }
    let els = [];
    try { els = [...document.querySelectorAll(target)].slice(0, 150); } catch (e) { /* unsupported */ }
    if (!active || !els.length) continue;
    for (const [offset, prop, without] of decls) {
      if (/^(transition|animation)/.test(prop)) continue;  // switched off above
      const names = longhands(prop);
      const snap = () => els.map(el => { const cs = getComputedStyle(el, pseudo || null);
        return names.map(n => cs.getPropertyValue(n)).join('|'); }).join('\\n');
      const before = snap();
      rule.style.cssText = without;
      const after = snap();
      rule.style.cssText = whole;
      tested.push(offset);
      if (before !== after) needed.push(offset);
    }
  }
  sheet.deleteRule(still);
  return [tested, needed];
}"""


def _save() -> None:
    if not os.environ.get("GARUDA_CHECK_CSS_AUDIT") or not (_used or _matched):
        return
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"coverage-{TAG}-{os.getpid()}.json").write_text(json.dumps({
        "ranges": sorted(_used), "selectors": sorted(_matched),
        "tested": sorted(_tested), "needed": sorted(_needed)}))


atexit.register(_save)


def instrument(browser) -> None:
    new_context, new_page = browser.new_context, browser.new_page

    def context_(*args, **kwargs):
        context = new_context(*args, **kwargs)
        _context(context)
        return context

    def page_(*args, **kwargs):
        page = new_page(*args, **kwargs)
        _context(page.context)
        return page

    browser.new_context, browser.new_page = context_, page_


def _context(context) -> None:
    if getattr(context, "_css_audit", False):
        return
    context._css_audit = True
    if BASELINE:
        text = Path(BASELINE).read_text()
        context.route(f"**{SHEET}?{MARK}*", lambda route: route.fulfill(
            status=200, body=text, headers={"Content-Type": "text/css; charset=utf-8"}))
    context.on("page", _page)
    for page in context.pages:
        _page(page)
    close = context.close

    def close_(*args, **kwargs):
        for page in context.pages:
            _flush(page)
        return close(*args, **kwargs)

    context.close = close_


def _page(page) -> None:
    if getattr(page, "_css_cdp", None):
        return
    cdp = page.context.new_cdp_session(page)
    sheets: dict[str, str] = {}
    cdp.on("CSS.styleSheetAdded", lambda e: sheets.__setitem__(
        e["header"]["styleSheetId"], e["header"].get("sourceURL", "")))
    cdp.send("DOM.enable")
    cdp.send("CSS.enable")
    cdp.send("CSS.startRuleUsageTracking")
    page._css_cdp, page._css_sheets = cdp, sheets
    for name in ("goto", "reload", "close", "go_back", "go_forward"):
        original = getattr(page, name)

        def wrapped(*args, _original=original, **kwargs):
            _flush(page)
            return _original(*args, **kwargs)

        setattr(page, name, wrapped)
    shot = page.screenshot

    def screenshot(*args, **kwargs):
        _flush(page)
        data = _compare(page, shot, kwargs) if BASELINE else shot(*args, **kwargs)
        if REDUNDANT:
            _redundant(page)
        return data

    page.screenshot = screenshot


def _flush(page) -> None:
    cdp = getattr(page, "_css_cdp", None)
    if not cdp or page.is_closed():
        return
    try:
        delta = cdp.send("CSS.takeCoverageDelta")
    except Exception:  # a crashed or detached target: nothing more to read
        return
    try:
        _matched.update(page.evaluate(MATCH, [[s, q] for s, q in _PROBES.items() if s not in _matched]))
    except Exception:  # mid-navigation: the next flush tries again
        pass
    for rule in delta.get("coverage", []):
        url = page._css_sheets.get(rule["styleSheetId"], "")
        if rule.get("used") and SHEET in url and MARK not in url:
            _used.add((int(rule["startOffset"]), int(rule["endOffset"])))


def _redundant(page) -> None:
    try:
        tested, needed = page.evaluate(REDUNDANT_JS, _PLAN)
        page.wait_for_timeout(300)  # an audit pause, not a wait: transitions the toggling started
    except Exception:  # mid-navigation: the next screenshot tries again
        return
    _tested.update(tested)
    _needed.update(needed)


def _compare(page, shot, kwargs):
    path = kwargs.pop("path", None)
    kwargs["animations"] = "disabled"
    current = page.evaluate("""() => {
      const link = document.querySelector('link[rel="stylesheet"]');
      return link ? link.getAttribute('href') : null;
    }""")
    if not current or not current.startswith(SHEET):
        data = shot(**kwargs)
        if path:
            Path(path).write_bytes(data)
        return data
    current = current.split("?")[0]
    record = {"check": TAG, "url": page.url, "path": str(path or ""), "viewport": page.viewport_size}
    for attempts in range(1, 4):  # noqa: B007 (reported below)
        new = shot(**kwargs)
        page.evaluate(SWAP, f"{SHEET}?{MARK}")
        base = shot(**kwargs)
        page.evaluate(SWAP, current)
        again = shot(**kwargs)
        if new == again:
            break
    record.update(same=new == base, stable=new == again, attempts=attempts)
    if not record["same"] or not record["stable"]:
        folder = OUT / "pixels"
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{TAG}-{next(_shots):03d}-" + re.sub(r"[^\w.-]+", "_", Path(path).stem if path else "shot")
        (folder / f"{stem}-base.png").write_bytes(base)
        (folder / f"{stem}-new.png").write_bytes(new)
        if not record["stable"]:
            (folder / f"{stem}-new2.png").write_bytes(again)
        record["files"] = stem
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / f"pixels-{TAG}-{os.getpid()}.jsonl").open("a") as handle:
        handle.write(json.dumps(record) + "\n")
    if path:
        Path(path).write_bytes(new)
    return new
