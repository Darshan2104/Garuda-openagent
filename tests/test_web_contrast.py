"""The dashboard's colour tokens meet WCAG AA contrast in both themes, by calculation.

The tokens live in ``static/app.css`` in three blocks: ``:root`` (light), the dark
``prefers-color-scheme`` override (``:root:not([data-theme="light"])``, so the toggle's light
choice wins), and ``html[data-theme="dark"]`` for the toggle's dark choice, which must repeat
the system dark block exactly. A theme block holds literal hex values; a few tokens are derived once, in ``:root``,
from others (``var(--x)``, and the state tints as ``color-mix(in srgb, <marker> <--tint>,
var(--surface))``), and this test resolves those exactly as the browser does (``in srgb``
mixes the gamma-encoded channels).

Checked, in both themes: every token drawn as text on every surface it sits on, each state's
word on its own tint, body text on every tint (banners, attention rows), text on a coloured
fill (buttons, badges, pipeline numbers, toast), all at 4.5:1 (most of it is 11-13 px); and
the marks that carry meaning without words (state markers, control borders, the focus ring,
chart series) at 3:1. Run directly to print the table:

    python tests/test_web_contrast.py
"""

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "garuda" / "interfaces" / "web" / "static" / "app.css"

AA, UI = 4.5, 3.0
SURFACES = ("bg", "surface", "surface-2", "surface-3")
STATES = ("run", "wait", "queue", "done", "fail", "crash", "pause", "unknown")
FAMILIES = ("fam-read", "fam-write", "fam-exec", "fam-net", "fam-subagent", "fam-gate",
            "fam-plan", "fam-other")

#: Tokens drawn as text (pills, labels, chat roles, family names, links).
TEXT_TOKENS = ("text", "muted", "accent", "accent-strong", "ok", "warn", "err", *FAMILIES,
               *(f"st-{s}" for s in STATES))
BACKGROUNDS = SURFACES
#: (foreground, background, what) for text set on a coloured fill.
ON_COLOUR = (
    ("on-accent", "accent", "primary button"), ("on-accent", "accent-strong", "primary, hover"),
    ("accent", "accent-tint", "selected row, active menu item"),
    ("surface", "st-done", "Approve button"), ("surface", "st-wait", "approvals count badge"),
    ("surface", "st-done", "number in a done pipeline marker"),
    ("surface", "st-run-dot", "number in the running pipeline marker"),
    ("surface", "st-fail-dot", "number in a failed pipeline marker"),
    ("surface", "st-pause-dot", "number in a paused pipeline marker"),
    ("surface", "st-crash-dot", "number in a quarantined pipeline marker"),
    ("surface", "text", "active state filter chip"), ("bg", "text", "toast"),
    ("surface", "err", "error fill"), ("bg", "err", "error fill on the page"),
    *((("surface", fam, "glyph on a tool-family chip")) for fam in FAMILIES),
    *((f"st-{s}", f"st-{s}-bg", f"{s} word on its tint") for s in STATES),
    *(("text", f"st-{s}-bg", f"body text on a {s} tint") for s in STATES),
)
#: (mark, background, what) for non-text marks that carry meaning: 3:1.
MARKS = (
    *((f"st-{s}-dot", bg, f"{s} marker") for s in STATES
      for bg in ("surface", "surface-2", f"st-{s}-bg")),
    ("border-strong", "surface", "input / button border"), ("border-strong", "bg", "input border"),
    ("border-strong", "surface-2", "input border in a tinted card"),
    *(("focus", bg, "focus ring") for bg in SURFACES),
    ("accent", "surface", "active tab underline"),
    ("chart-1", "surface", "chart series 1"), ("chart-2", "surface", "chart series 2"),
    ("chart-5", "surface", "failed bar"),
)


def _declarations(body: str) -> dict[str, str]:
    return {name: " ".join(value.split())
            for name, value in re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", body)}


def _block(css: str, selector: str) -> dict[str, str]:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, selector
    return _declarations(match.group(1))


def blocks(css: str | None = None) -> dict[str, dict[str, str]]:
    """The raw declarations of each theme: the toggle blocks layered over the system ones."""
    css = css if css is not None else CSS.read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    root = _block(css, ":root")
    media = css[css.index("@media (prefers-color-scheme: dark)"):]
    # The system dark theme yields to the toggle's light choice, so no light block repeats.
    dark_media = _block(media, ':root:not([data-theme="light"])')
    dark_toggle = _block(css, 'html[data-theme="dark"]')
    return {"light": root, "dark": {**root, **dark_toggle},
            "light (system)": root, "dark (system)": {**root, **dark_media}}


def _hex(rgb) -> str:
    return "#" + "".join(f"{round(c):02x}" for c in rgb)


def resolve(tokens: dict[str, str], name: str, depth: int = 0) -> str:
    """A token's colour as ``#rrggbb``: a literal, a ``var()`` of another token, or a
    ``color-mix(in srgb, A p%, B)`` of two of them."""
    assert depth < 10, f"--{name} refers to itself"
    value = tokens[name]
    return _colour(tokens, value, depth)


def _colour(tokens: dict[str, str], value: str, depth: int) -> str:
    value = value.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value.lower()
    var = re.fullmatch(r"var\(--([a-z0-9-]+)\)", value)
    if var:
        return resolve(tokens, var.group(1), depth + 1)
    mix = re.fullmatch(r"color-mix\(in srgb,\s*(.+?)\s+(var\(--[a-z0-9-]+\)|[\d.]+%),\s*(.+)\)", value)
    assert mix, f"cannot resolve {value!r}"
    first, amount, second = mix.groups()
    if amount.startswith("var("):
        amount = tokens[amount[6:-1]]
    p = float(amount.rstrip("%")) / 100
    a, b = (_colour(tokens, part, depth + 1) for part in (first, second))
    channels = [int(a[i:i + 2], 16) * p + int(b[i:i + 2], 16) * (1 - p) for i in (1, 3, 5)]
    return _hex(channels)


def themes(css: str | None = None) -> dict[str, dict[str, str]]:
    """Every colour token of each theme, resolved to hex."""
    out = {}
    for theme, tokens in blocks(css).items():
        # Opaque colours only: a mix with `transparent` (the focus halo) is drawn over
        # whatever is below it and is not a token any pair here uses.
        out[theme] = {name: resolve(tokens, name) for name, value in tokens.items()
                      if value.startswith(("#", "var(--", "color-mix(in srgb"))
                      and "transparent" not in value}
    return out


def luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def ratio(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def pairs(tokens: dict[str, str]):
    """(foreground, background, ratio, minimum, what) for every pair the stylesheet draws."""
    for fg in TEXT_TOKENS:
        for bg in BACKGROUNDS:
            yield fg, bg, ratio(tokens[fg], tokens[bg]), AA, "text on a surface"
    for fg, bg, what in ON_COLOUR:
        yield fg, bg, ratio(tokens[fg], tokens[bg]), AA, what
    for fg, bg, what in MARKS:
        yield fg, bg, ratio(tokens[fg], tokens[bg]), UI, what


def test_the_toggle_block_repeats_the_system_dark_theme():
    found = blocks()
    assert found["dark"] == found["dark (system)"]


def test_every_text_and_mark_meets_its_minimum_in_both_themes():
    failures = [f"{theme}: --{fg} on --{bg} is {value:.2f}:1 < {minimum} ({what})"
                for theme in ("light", "dark")
                for fg, bg, value, minimum, what in pairs(themes()[theme]) if value < minimum]
    assert not failures, failures


def test_every_token_the_stylesheet_uses_is_defined():
    css = CSS.read_text(encoding="utf-8")
    used = set(re.findall(r"var\(--([a-z0-9-]+)", css))
    defined = set(re.findall(r"--([a-z0-9-]+)\s*:", css))
    assert not used - defined, sorted(used - defined)


def test_the_resolver_mixes_like_the_browser():
    tokens = {"a": "#ffffff", "b": "#000000", "t": "25%", "m": "color-mix(in srgb, var(--a) var(--t), var(--b))"}
    assert resolve(tokens, "m") == "#404040"


def test_the_check_catches_the_old_light_warn_colour():
    assert ratio("#a07217", "#f4f4f2") < AA


if __name__ == "__main__":
    for theme in ("light", "dark"):
        print(f"\n{theme}")
        for fg, bg, value, minimum, what in pairs(themes()[theme]):
            print(f"  --{fg:<15} on --{bg:<15} {value:5.2f}:1 {'ok' if value >= minimum else 'FAIL'}"
                  f"  ({minimum}) {what}")
