"""The dashboard's colour tokens meet WCAG AA contrast in both themes, by calculation.

The tokens live in ``static/app.css`` in four blocks: ``:root`` (light), the dark
``prefers-color-scheme`` override, and the two ``html[data-theme]`` blocks the theme toggle
sets. Every token used for text is checked against every background it is drawn on at
4.5:1 (most of it is 11-13 px), and the light-on-colour pairs (primary button, count badge,
toast, active chip, live pill) the same way. Run directly to print the table:

    python tests/test_web_contrast.py
"""

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "garuda" / "interfaces" / "web" / "static" / "app.css"

#: Tokens drawn as text (pills, labels, chat roles, family names, links).
TEXT_TOKENS = ("text", "muted", "accent", "ok", "warn", "err", "fam-read", "fam-write",
               "fam-exec", "fam-net", "fam-subagent", "fam-gate", "fam-plan", "fam-other")
BACKGROUNDS = ("bg", "surface", "surface-2")
#: (foreground, background) for text set on a coloured fill.
ON_COLOUR = (("surface", "accent"), ("surface", "err"), ("bg", "err"), ("bg", "text"))
AA = 4.5


def _block(css: str, selector: str) -> dict[str, str]:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, selector
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", match.group(1)))


def themes(css: str | None = None) -> dict[str, dict[str, str]]:
    css = css if css is not None else CSS.read_text(encoding="utf-8")
    root = _block(css, ":root")
    media = css[css.index("@media (prefers-color-scheme: dark)"):]
    dark_media = _block(media, ":root")
    light_toggle = _block(css, 'html[data-theme="light"]')
    dark_toggle = _block(css, 'html[data-theme="dark"]')
    return {"light": {**root, **light_toggle}, "dark": {**root, **dark_media, **dark_toggle},
            "light (system)": root, "dark (system)": {**root, **dark_media}}


def luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def ratio(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def pairs(tokens: dict[str, str]):
    for fg in TEXT_TOKENS:
        for bg in BACKGROUNDS:
            yield fg, bg, ratio(tokens[fg], tokens[bg])
    for fg, bg in ON_COLOUR:
        yield fg, bg, ratio(tokens[fg], tokens[bg])


def test_the_toggle_blocks_repeat_the_system_themes():
    found = themes()
    assert found["light"] == found["light (system)"]
    assert found["dark"] == found["dark (system)"]


def test_every_text_token_meets_aa_on_every_surface_in_both_themes():
    failures = [f"{theme}: --{fg} on --{bg} is {value:.2f}:1"
                for theme in ("light", "dark")
                for fg, bg, value in pairs(themes()[theme]) if value < AA]
    assert not failures, failures


def test_the_check_catches_the_old_light_warn_colour():
    assert ratio("#a07217", "#f4f4f2") < AA


if __name__ == "__main__":
    for theme in ("light", "dark"):
        print(f"\n{theme}")
        for fg, bg, value in pairs(themes()[theme]):
            print(f"  --{fg:<13} on --{bg:<10} {value:5.2f}:1 {'ok' if value >= AA else 'FAIL'}")
