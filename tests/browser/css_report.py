"""Report on a ``css_audit.py`` run: unused rules and selectors, redundant declarations, pixels.

    python tests/browser/css_report.py AUDIT_DIR [--css garuda/interfaces/web/static/app.css]

Pass ``--css`` the stylesheet the run served (offsets are into that text). Evidence per rule:

* **coverage**: Chromium marked the rule used on some page of the walk (a rule in a media
  query that never applied, or for an element that never rendered, stays unused);
* **DOM**: each selector of the rule's list matched some element at some point of the walk;
* **source**: every class, id and attribute value the selector names occurs as text in the
  static JavaScript or index.html (a class built by concatenation, ``"pipe-" + key``, does not,
  so a selector flagged only by this needs reading the code);
* **redundant** (if the run had ``GARUDA_CHECK_CSS_REDUNDANT=1``): taking the declaration out
  never changed a computed value where it was tried.

None of this is proof on its own: a selector is removable when it never matched, its rule was
never used and no code path can emit it; a declaration when the code says why it never counts.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
from pathlib import Path

from css_audit import split_list, style_rules

STATIC = Path(__file__).resolve().parents[2] / "garuda/interfaces/web/static"


def source_missing(selector: str, source: str) -> list[str]:
    plain = re.sub(r"\[[^\]]*\]", "", selector)
    names = set(re.findall(r"\.([a-zA-Z_][\w-]*)", plain)) | set(re.findall(r"#([a-zA-Z_][\w-]*)", plain))
    missing = sorted(n for n in names if n not in source)
    for attr, value in re.findall(r"\[([\w-]+)(?:[~^$*|]?=\"([^\"]*)\")?\]", selector):
        if attr not in source or (value and value not in source):
            missing.append(f"[{attr}={value}]" if value else f"[{attr}]")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audit", type=Path)
    parser.add_argument("--css", type=Path, default=STATIC / "app.css")
    args = parser.parse_args()
    text = args.css.read_text()
    source = "".join(p.read_text() for p in [*STATIC.glob("*.js"), STATIC / "index.html"])
    ranges, matched, tested, needed = set(), set(), set(), set()
    for f in args.audit.glob("coverage-*.json"):
        data = json.loads(f.read_text())
        ranges.update(map(tuple, data["ranges"]))
        matched.update(data["selectors"])
        tested.update(data.get("tested", []))
        needed.update(data.get("needed", []))
    starts = sorted(s for s, _ in ranges)

    def used(start, end):
        k = bisect.bisect_left(starts, start)
        return k < len(starts) and starts[k] < end

    def where(rule):
        return (f"@{rule['media']} " if rule["media"] else "") + " ".join(rule["head"].split())

    rules = style_rules(text)
    unused = [r for r in rules if not used(r["start"], r["end"])]
    print(f"{len(rules)} rules; {len(unused)} never used ({sum(r['end'] - r['start'] for r in unused)} bytes)\n")
    print("== selectors that never matched the DOM [rule used?] absent tokens ==")
    for rule in rules:
        for part in split_list(rule["head"]):
            if part not in matched:
                missing = source_missing(part, source)
                print(f"  {where({**rule, 'head': part})}   [{'used' if used(rule['start'], rule['end']) else 'UNUSED'}]"
                      + (f"  absent: {', '.join(missing)}" if missing else ""))
    print("\n== rules never used although a selector matched (media, pointer states) ==")
    for rule in unused:
        if any(p in matched for p in split_list(rule["head"])):
            print(f"  {where(rule)}")
    if tested:
        spare = [(rule, decl) for rule in rules for off, decl in rule["decls"] if off in tested and off not in needed]
        print(f"\n== declarations that never changed a computed value ({len(spare)} of {len(tested)} tried) ==")
        for rule, decl in spare:
            print(f"  {where(rule)[:100]}  |  {decl}")
    shots = [json.loads(line) for f in args.audit.glob("pixels-*.jsonl") for line in f.read_text().splitlines()]
    differ = [s for s in shots if not s["same"]]
    unstable = [s for s in shots if not s["stable"]]
    print(f"\n== pixels: {len(shots)} screenshots compared, {len(differ)} differ, {len(unstable)} unstable ==")
    for s in differ + [s for s in unstable if s["same"]]:
        print(f"  {'DIFF' if not s['same'] else 'UNSTABLE'} {s['check']} {s['url']} {s['viewport']} "
              f"{s.get('files', '')} stable={s['stable']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
