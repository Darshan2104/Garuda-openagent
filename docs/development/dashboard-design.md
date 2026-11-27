# Dashboard design

The web dashboard (`garuda web`) has one stylesheet,
`garuda/interfaces/web/static/app.css`, and one visual language. This page is that
language: the tokens, the components and the rules a new page follows so it looks like the
others without rules of its own. The user-facing summary of the state colours is in the
[web dashboard guide](../guides/web-dashboard.md#how-state-is-drawn).

Constraints that shape everything: plain `<script>` files and no build step; a strict CSP
(no inline `style` attributes, no external hosts; `data:` images only), so JavaScript never
sets a colour or a size, it emits classes and `data-*` attributes; and the product's honesty
rules, which the design must make visible rather than smooth over.

## Principles

- **State is drawn one way everywhere.** A marker with its own colour *and* shape, and the
  word. Pills, state chips, pipeline markers, row stripes and the Overview's attention rows
  all read the same variables, so "waiting" looks the same on every page.
- **Unknown is visibly unknown.** A hollow grey ring and the word "unknown" or "not
  recorded"; never green, never zero, never blank.
- **Outcome and verification are two indicators**, and a review has its own chip that is
  never styled or worded as verification. An adapter-reported cost says so.
- **Calm metadata, loud state.** One accent (indigo) for interaction; status colours only for
  state. Chips are borderless `key: value` text with a quiet key; tags (flow, consult,
  example) are outlined with no marker so they are not mistaken for states.
- **Text does not change for style.** Browser checks read `innerText`; restyling keeps every
  word, and a chip stays inline so `verification: unavailable` stays on one line.

## Tokens

Every value is a custom property. `:root` is the light theme; the dark theme is one block of
literal values written twice, under `@media (prefers-color-scheme: dark)` as
`:root:not([data-theme="light"])` (so the toggle's light choice wins) and as
`html[data-theme="dark"]` (the toggle's dark choice). The two must match;
`tests/test_web_contrast.py` checks that, resolves the derived tokens and checks every
text/background pair at 4.5:1 and every meaningful mark at 3:1 in both themes.

Derived tokens are defined once, in `:root`, from the theme's literals: `--ok`, `--warn`,
`--err` and `--focus` are aliases; the eight state tints are
`--st-<state>-bg: color-mix(in srgb, var(--st-<state>-dot) var(--tint), var(--surface))`, with
`--tint` 10% light and 14% dark; `--halo` is the focus colour at 30%.

### Surfaces and text

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#f6f7f9` | `#0d0f13` | page canvas |
| `--surface` | `#ffffff` | `#15181e` | cards, tables, inputs |
| `--surface-2` | `#f2f4f7` | `#1b1f26` | table headers, code, hover rows, quiet panels |
| `--surface-3` | `#e8ebf0` | `#232831` | pressed, meter track, skeleton |
| `--border` | `#dfe3e9` | `#2b313b` | hairlines (decorative) |
| `--border-strong` | `#7f8998` | `#6b7687` | input and button edges (3:1 on every surface) |
| `--text` / `--muted` | `#151a22` / `#556070` | `#e6e9ef` / `#9da6b4` | text; secondary text (still AA) |
| `--accent` | `#3451c6` | `#8ea3ff` | links, primary button, active tab, focus |
| `--accent-strong` / `--accent-tint` | `#2a43a8` / `#edf1fd` | `#aebcff` / `#1d2544` | hover; selected row, active menu item |

### States

Each state has a text colour (`--st-<s>`), a marker colour (`--st-<s>-dot`), a derived tint
(`--st-<s>-bg`) and a marker shape. The word is always printed.

| State | `<s>` | Shape | Used for |
|---|---|---|---|
| running | `run` | solid dot with a halo (pulses unless reduced motion) | working, live, the step in flight |
| waiting on you | `wait` | solid dot with a halo | waiting sessions, approvals, the nav count |
| queued | `queue` | ring with a solid edge | queued sessions and runs |
| completed | `done` | solid dot | completed, done, passed |
| failed | `fail` | solid dot | failed, refused, errors |
| crashed | `crash` | diamond (a square marker for a quarantined step) | crashed sessions |
| paused | `pause` | two bars | a flow stopped on a resumable pause |
| unknown | `unknown` | hollow ring | unknown, not recorded, anything unrecognised |

Cancelled and stopped (a known end that is neither) use the unknown colours with a solid dot.
Tool families (`--fam-read`, `--fam-write`, `--fam-exec`, `--fam-net`, `--fam-subagent`,
`--fam-gate`, `--fam-plan`, `--fam-other`) say what kind of call a row was, never whether it
worked; charts use `--chart-1` … `--chart-5`, never status colours as series.

### Type, space, shape, motion

- System stacks only: `--sans`, and `--mono` for ids, paths, commands, code and raw payloads,
  never for labels or states. Numbers use `tabular-nums`.
- Sizes: page title 22/650; stat value 26/650; card title 15/600; body 14; tables 13;
  meta 12.5; pills, chips, table headers 12; eyebrow labels (`.block-label`,
  `.section-label`, `.eyebrow`) 11/600 uppercase.
- Spacing on a 4 px grid: `--s1` 4 · `--s2` 8 · `--s3` 12 · `--s4` 16 · `--s5` 20 ·
  `--s6` 24 · `--s8` 32. Page gutter 32 (16 at ≤ 900 px); card padding 16×20 (14×16 on a
  phone); 16 between cards.
- Radius: `--radius-sm` 4 (code), `--radius` 6 (buttons, inputs, notices), `--radius-lg` 10
  (cards, tables, approval cards), 999 (pills, filter chips). `--shadow-1` on cards,
  `--shadow-2` only on floating things (phone menu, keyboard help, toast).
- Motion: `--fast` 120 ms, `--slow` 220 ms. `prefers-reduced-motion` stops every animation;
  state stays readable from colour, shape and word.

## Components

All styled by class, so reusing the markup is enough.

| Component | Markup | Notes |
|---|---|---|
| Page head | `.page-head > h1`, then `.meta`, links, `.btn`s | The first button pushes the actions right; a leading `<a>` is a breadcrumb. Put a `.page-lede` or `.stat-sub` under it to say what the page is for. |
| Card | `.card` (`> h2` title) | `.grid.cols-2` / `.grid.cols-4` lay cards out; `.card.table-wrap` lets a table reach the card's edges. |
| Stat tile | `.card[data-tile]` or `.tile` with `.stat-label`/`.tile-label`, `.stat-value`/`.tile-value`, `.stat-sub` | A qualifier inside a value goes in `.tile-qual` (set under the number). |
| State pill | `.pill` + `running` · `success` · `failed` · `paused` · `unknown` | Or `data-state` / `data-status` with the raw word (`queued`, `waiting`, `crashed`, `cancelled`). With no class it is unknown. |
| Chip | `chip(label, value, cls)` in `views_sessions.js` | Renders `<span class="chip" data-chip="label"><span class="chip-k">label:</span> value</span>`; `chip-on`, `chip-bad`, `chip-pause`, `chip-off`. State chips (outcome, verification, review, status, …) get the marker; others are plain metadata. Several chips go in a `.chips` row. |
| Tag | `.pill.unknown` inside `.s-name`, `.origin-pill`, … | Outlined, no marker. |
| Notice / banner | `.notice` + `err` · `warn` · `approval` · `paused`, with `.notice-title`, `.notice-body` | A tinted box with a state stripe. `.conn-banner` is the app-wide one. |
| Buttons | `.btn`, `.btn-primary`, `.btn-danger`, `.btn-small` | `aria-busy="true"` shows a spinner; Approve/Deny are told apart by `data-approve`/`data-deny`/`data-act`/`data-flow-approval` (filled green ✓ vs outlined red ✕). |
| Form | inputs, `select`, `textarea`; `.card label` stacks label over field | Focus: an accent border and a `--halo` ring. `.starter-form` / `.starter-result` lay out a form and its result; `.starter-preview` is the preview panel under a form. |
| Table | `table` in `.table-wrap`; `th.num`/`td.num` right-align numbers; `tfoot` totals | Add `.table-stack` and `data-th="Column"` on each cell: at ≤ 760 px each row becomes a card of labelled values (the first cell is its title). |
| Flow pipeline | `ol.pipeline > li.pipe-step.pipe-{done,failed,current,pending}` with `.pipe-head` (`.pipe-no`, `strong`, `.pill`), `ul.attempts > li.attempt` | `data-status` (the last attempt's status) and `data-pause` (its pause kind) on the step draw a paused or quarantined step; `data-pause` on an attempt draws its stop as a pause. `.flow-callout` holds review/quota lines, `.pipe-pass` and `.flow-edges` what it passed on. |
| Approval card | `ul.approvals > li.approval-item` | The requested action (`pre`) is the largest thing; `data-state` after an answer recolours the stripe. |
| Empty / loading | `.empty` (`strong` + `p`), `.loading`, `p.loading` | A skeleton of the shape about to arrive; the words stay. |
| Trace page | `.trace-page` column of regions; `.turn`, `.step.step-<family>`, `.block`, `.gate` | Spacing is the column gap, so an empty region takes no room. |

## Adding a page

1. Build it from the components above: a `.page-head`, cards, `.chips`, `.pill`s and
   `.table-stack` tables. Most pages need no CSS at all.
2. If it needs rules, put them in **one marked block** under `/* ===== pages */`
   (`/* --- <Page> --- */`), scoped by the page's root id or a page class, using tokens only:
   no hex values, no pixel colours, no new spacing values off the grid.
3. A new state word maps to an existing state (by class or `data-state`); never invent a
   colour. If it is genuinely new, add a token pair to all three theme blocks, a shape, a row
   to the tables here and in the guide, and pairs to `tests/test_web_contrast.py`.
4. Phone: nothing may scroll sideways at 390 px. Tables stack with `.table-stack`; long ids
   and paths wrap (`overflow-wrap: anywhere`) or are shown by their tail with the full value
   in a `title`.
5. Every interactive element keeps the global `:focus-visible` ring; do not set
   `outline: none` without a replacement.
6. Add the page's route to `ROUTES` in `tests/browser/check_design.py`, run
   `python tests/test_web_contrast.py` if tokens changed, and the browser suite.

The Flows page (`static/views_flows.js`) reuses the pipeline, card, form, preview and banner
classes this way; its own rules live in its one marked block.

## Checks

- `tests/test_web_contrast.py`: every token pair, resolved from the real file, both themes.
- `tests/browser/check_design.py`: on every route, no sideways scroll at 390 px, no unstyled
  pill, chips on one line, a focus ring on everything Tab reaches, and a paused flow drawn as
  paused.
- `tests/test_web_static.py`: the CSP and the static file list.
