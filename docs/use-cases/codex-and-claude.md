# Worked example: Codex and Claude Code together

This page runs every packaged workflow, plus three custom flows, on two small
projects using only subscription logins: Codex (ChatGPT) and Claude Code. A
bigger Codex model plans and reviews, and a smaller one scouts and codes.
Claude Sonnet plans, builds or reviews across vendors. Each section shows the
command, what happened when it ran, and where to see it in the dashboard.

!!! note "Tested with"
    Codex adapter `@agentclientprotocol/codex-acp` 2.1.1 (`gpt-6.1-sol` and
    `gpt-5.6-sol`, effort `high`) and Claude Code adapter
    `@agentclientprotocol/claude-agent-acp` 0.85.0 (`sonnet`, which ran as
    Sonnet 5.5), on macOS. Models write different code on every run; the
    results below are examples, not fixtures.

## 1. Set up once

Install the adapter versions on which Garuda has proven it can set a role's
model, effort and [harness mode](../guides/configuration.md#a-roles-harness-mode).
A newer adapter refuses a role that names any of them (`role.options_unproven`)
before anything is prompted.

```bash
npm install -g @agentclientprotocol/codex-acp@2.1.1 @agentclientprotocol/claude-agent-acp@0.85.0
codex login status
claude auth status
garuda doctor --runtime codex
garuda doctor --runtime claude
```

`garuda doctor` should show `codex 2.1.1 … logged in` and
`claude 0.85.0 … logged in`. Garuda reads only the status commands' answers,
never either login. Don't set `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` when you
mean to use the subscriptions.

Save the roles and flows below as your user `garuda.yaml`. `garuda doctor`
prints the path it reads: normally `~/.agent/garuda.yaml`, or
`~/.garuda/garuda.yaml` when only that folder exists.

```yaml
version: 1
defaults: {role: coder}
harnesses:
  codex: {allowed_models: [gpt-6.1-sol, gpt-5.6-sol]}
  claude: {allowed_models: [sonnet]}
roles:
  # Codex: the bigger model plans and reviews, the smaller one scouts and codes.
  scout:    {harness: codex, model_id: gpt-5.6-sol, effort: high, permissions: smart,
             write_policy: no-edits, agent: read-only}
  planner:  {harness: codex, model_id: gpt-6.1-sol, effort: high, permissions: smart,
             write_policy: no-edits, agent: read-only}
  coder:    {harness: codex, model_id: gpt-5.6-sol, effort: high, permissions: auto}
  reviewer: {harness: codex, model_id: gpt-6.1-sol, effort: high, permissions: smart,
             write_policy: no-edits, agent: read-only}
  # Claude Code: a second vendor that plans, builds or reviews.
  claude-planner:  {harness: claude, model_id: sonnet, effort: high, permissions: smart,
                    write_policy: no-edits, agent: read-only}
  claude-coder:    {harness: claude, model_id: sonnet, effort: high, permissions: smart}
  claude-reviewer: {harness: claude, model_id: sonnet, effort: high, permissions: smart,
                    write_policy: no-edits, agent: read-only}
flows:
  scout-fix-review:
    description: Codex scouts and fixes, Claude reviews and can send it back.
    steps:
      - {id: scout, role: scout, write_policy: no-edits, outputs: [notes]}
      - {id: fix, role: coder, inputs: [notes], outputs: [patch],
         review: {by: claude-reviewer, max_rounds: 2}}
      - {id: review, role: claude-reviewer, write_policy: no-edits, inputs: [patch], outputs: [review]}
  cross-vendor-build:
    description: Claude plans, Codex builds, a bigger Codex model reviews and can send it back.
    steps:
      - {id: plan, role: claude-planner, write_policy: no-edits, outputs: [plan]}
      - {id: build, role: coder, inputs: [plan], outputs: [patch],
         review: {by: reviewer, max_rounds: 2}}
      - {id: review, role: reviewer, write_policy: no-edits, inputs: [patch], outputs: [review]}
  claude-builds:
    description: Codex plans, Claude builds, Codex reviews and can send it back.
    steps:
      - {id: plan, role: planner, write_policy: no-edits, outputs: [plan]}
      - {id: build, role: claude-coder, inputs: [plan], outputs: [patch],
         review: {by: reviewer, max_rounds: 2}}
      - {id: review, role: reviewer, write_policy: no-edits, inputs: [patch], outputs: [review]}
```

The Claude adapter offers model aliases (`sonnet`, `opus`, `haiku`), not full
IDs, so `sonnet` is the exact value to use. Each reviewer runs as a different
harness and model from the role it reviews, so every review is
[independent](teams.md#run-a-plan-build-review-flow).

No role names a `harness_mode`, so each runs in the mode its step allows: a
Claude coder in `acceptEdits`, the Codex coder in `agent` (its
`permissions: auto` lets Codex run tests without asking), and every no-edits
step in Claude's `default` or Codex's `read-only`. Under `permissions: smart`
a Codex coder would run in `workspace-write` and be refused the test runs it
asks for. Section 5 shows how to switch.

The no-edits roles use a small [agent definition](../guides/agents.md). Save it
as `read-only.yaml` in the `agents/` folder next to your `garuda.yaml`:

```yaml
version: 1
description: Investigate or review without writing anything in the workspace
instructions:
  mode: append
  text: |
    This step must leave the workspace byte-for-byte unchanged, including
    ignored files and caches. Read files and diffs instead of running commands.
    If you must run tests, make them write nothing, for example
    `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider`.
    Never run builds, formatters, installers or git commands that write.
```

Why it's needed: in `read-only` mode Codex asks before a command that writes,
such as a test run that updates `.pytest_cache/`. Garuda refuses it on a
no-edits step, and Codex then ends its turn, so the step fails with
`flow.step_failed: … after Garuda denied: Run command`. Reading instead of
running keeps the review.

## 2. Get the two example projects

The reconnect project ships with Garuda:

```bash
garuda starter example reconnect ~/garuda-reconnect
```

The second is a word counter with a real bug: `The` and `the`, and `cat.` and
`cat`, are counted as different words.

```bash
mkdir ~/garuda-wordfreq && cd ~/garuda-wordfreq
cat > wordfreq.py <<'EOF'
"""Print the most common words in a text file."""
import argparse
import sys
from collections import Counter


def words(text):
    return text.split()


def top_words(text, n=10):
    return Counter(words(text)).most_common(n)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("-n", type=int, default=10, help="how many words to show")
    args = parser.parse_args(argv)
    with open(args.path, encoding="utf-8") as handle:
        for word, count in top_words(handle.read(), args.n):
            print(f"{count}\t{word}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
EOF
cat > test_wordfreq.py <<'EOF'
from wordfreq import top_words


def test_counts_repeated_words():
    assert top_words("a b a", 1) == [("a", 2)]


def test_limits_results():
    assert len(top_words("a b c d", 2)) == 2
EOF
printf 'The cat sat. The cat ran!\nthe end.\n' > sample.txt
printf '__pycache__/\n.pytest_cache/\n' > .gitignore
git init -q && git add . && git commit -qm "wordfreq example"
python wordfreq.py sample.txt
```

Commit each workflow's result before starting the next one, as you would in
your own work.

## 3. The packaged workflows

Run these in `~/garuda-reconnect`. `garuda starter list` should show all five
starters as `ready`.

| Workflow | Roles that ran | What happened |
|---|---|---|
| Ask a role | reviewer (Codex 6.1) | Answered from `client.py` with line citations, and pointed out that the question used the wrong method name. Nothing changed. |
| Plan a change | scout (Codex 5.6) → planner (Codex 6.1) | Notes, then a plan with a transition table and test list. Nothing changed. |
| Build and review the plan | coder (Codex 5.6) ⇄ reviewer (Codex 6.1) | Status tracking implemented, 6 → 21 tests, approved in round 1. |
| Plan from feedback | scout → planner | A plan to expose the last failure reason and attempt count. |
| Run with a role | coder (Codex 5.6) | Asked one clarifying question; a follow-up implemented it with tests. |
| Plan, build, review | planner → coder ⇄ reviewer | `--json` output for wordfreq, 11 tests, approved in round 1. |

**Ask a role** (read-only; the default `reviewer`):

```bash
garuda starter run ask-role --question "What does reconnect() do when every attempt fails, and how many attempts are made by default?" --source client.py
```

**Plan a change**, then implement that exact plan with an independent review:

```bash
garuda starter run plan-change --goal "Add reconnect status using the current client" --requirements "Expose disconnected/reconnecting/connected status; cover every transition with deterministic tests" --exclude "Transport rewrite" --constraints "Preserve retry attempts and delays" --source client.py
garuda starter result FLOW_ID --json
garuda starter run build-review --variant pair --plan-artifact FLOW_ID:plan:1
```

The first command ends by printing the `build-review` command with your
`FLOW_ID` filled in. Planning changes nothing; implementation starts only when
you run that command.

**Plan from feedback:**

```bash
garuda starter run plan-feedback --feedback "Users cannot tell why the client stays disconnected after retries run out" --current "Status becomes disconnected with no reason" --desired "Expose the last failure reason and attempt count after exhaustion" --constraints "Preserve retry attempts, delays and the status values" --source client.py
```

**Run with a role**, and answer a question the agent asks:

```bash
cd ~/garuda-wordfreq
garuda starter run run-with-role --goal "Add a --min-length N option that ignores words shorter than N characters" --requirements "Default keeps every word; add tests" --constraints "Keep the default output unchanged" --source wordfreq.py
garuda run --resume SESSION_ID -t "Yes: count length after normalization. Implement it now with tests; no more questions."
```

Codex's own skills can make it ask before editing. The follow-up continues on
Codex through a brief of the first session.

**Plan, build, review** as a single flow:

```bash
garuda flow run plan-build-review -t "Add a --json flag that prints the top words as a JSON list of {\"word\": ..., \"count\": ...} objects in the same order, keep the default tab-separated output unchanged, and add tests for both outputs."
```

## 4. Custom flows: Codex and Claude working together

In a flow, roles pass typed artifacts to each other: notes, a plan, a patch,
then a review. When a review asks for changes, its findings go back to the
coder, and the reviewer looks again, up to `max_rounds` more times.

| Flow | Who did what | What happened |
|---|---|---|
| `scout-fix-review` | Codex scouts and fixes, Claude reviews | Fixed the counting bug; Claude approved in round 1 with real minor findings |
| `cross-vendor-build` | Claude plans, Codex builds, Codex reviews | Round 1 requested changes (a real bug); fixed; approved in round 2 |
| `claude-builds` | Codex plans, Claude builds in `acceptEdits`, Codex reviews | `--stopwords` option, 23 tests, approved in round 1 |
| `plan-build-review` | Codex plans, builds, reviews | Three rounds of floating-point findings, then `flow.review_changes_requested` |

**Codex fixes, Claude reviews** (in `~/garuda-wordfreq`):

```bash
garuda flow run scout-fix-review -t "Bug report: wordfreq counts 'The' and 'the' as different words, and keeps punctuation, so 'cat.' and 'cat' are counted separately. Fix the counting so case and surrounding punctuation are ignored, keep apostrophes inside words (don't), and add tests."
```

The Codex scout found the cause and Codex fixed `words()`. Claude approved in
round 1 with one minor and two nit findings. The minor one: `string.punctuation`
covers ASCII only, so curly quotes and em dashes are still counted. The output
became `3 the`, `2 cat`.

**Claude plans, Codex builds, Codex reviews** (in `~/garuda-reconnect`):

```bash
garuda flow run cross-vendor-build -t "After retries run out, expose why: record the last connection failure reason and the number of attempts made, reset both on the next successful connect, and add deterministic tests. Preserve retry attempts, delays and the existing status values."
```

This flow needed two rounds. In round 1 the Codex reviewer requested changes
with a `[major]` finding: the diagnostics were cleared before a successful
connection, contrary to the plan. The coder fixed that and added a test, and
round 2 approved.

**Codex plans, Claude builds, Codex reviews** (in `~/garuda-wordfreq`):

```bash
garuda flow run claude-builds -t "Add a --stopwords FILE option: words listed in FILE (one per line, compared after the same normalization) are not counted. Default behavior is unchanged. Add tests."
```

Claude edited the files itself in `acceptEdits` mode, and the Codex reviewer
approved in round 1.

**When the reviewer keeps asking.** A reviewer is not obliged to approve. In
this run on `~/garuda-reconnect`, the Codex reviewer found a new floating-point
edge case in each of three rounds, and the flow stopped:

```bash
garuda flow run plan-build-review -t "Add a max_total_delay option: reconnect() stops retrying once the sum of delays would exceed it, records 'gave up after max_total_delay' as the failure reason, and leaves other behaviour unchanged. Add deterministic tests."
```

```text
[garuda] review_changes_requested after 3 round(s) (a review, not verification)
{"flow_session": "…", "state": "stopped", "code": "flow.review_changes_requested"}
```

The changes stay in your working tree for you to finish or discard. Review
approval is not verification either way: to have Garuda run your tests against
the result, add `--check "python -m pytest"`. That needs the Linux supervisor;
see [Level 5](teams.md#run-a-plan-build-review-flow).

## 5. Switch harness modes

Name a mode on the role, or switch it for one run with `--harness-mode`. These
were run in `~/garuda-wordfreq`:

```bash
garuda run --role claude-coder -t "Create a file NOTE.txt containing the word hello. Do nothing else."
garuda run --role claude-coder --harness-mode default -t "Create a file NOTE.txt containing the word hello. Do nothing else."
garuda run --role coder --harness-mode agent-full-access -t "…"
```

- The first ran in `acceptEdits` and wrote the file.
- The second ran in Claude Code's ask-first `default`. A headless run answers
  no approvals, so `Write NOTE.txt` was denied and no file was written. Run it
  in an interactive terminal to answer the request yourself.
- The third refused before launch: `role.mode_exceeds_permissions:
  agent-full-access on codex needs permissions: yolo or higher, more than
  permissions: auto allows`.

To keep a mode, put it on the role:

```yaml
roles:
  careful-coder: {harness: claude, model_id: sonnet, effort: high, harness_mode: default}
```

See [A role's harness mode](../guides/configuration.md#a-roles-harness-mode)
for every mode, what it needs and the defaults.

## 6. Watch it in the dashboard

```bash
garuda web --read-only --allow-workspace ~/garuda-reconnect
```

Open the printed token URL.

- **Sessions** lists every step session, with its runtime, role and model,
  such as `codex · role coder · acp:codex/gpt-5.6-sol`. A flow's page shows
  each step and attempt, the artifacts passed between them, whether each
  attempt changed the workspace, every recorded identity, and the review
  outcome, such as **approved after 2 rounds**.
- **Runs**, for a Codex or Claude session, shows an **External harness** card:
  harness and adapter version, role, model, effort, the harness mode that
  ran, the write policy, per-turn counts of message events, tool calls, tool
  results, approval requests and errors, and each approval with its outcome.
  **Models used** shows the context snapshot each turn reported. For Claude,
  that includes the model that actually ran (`claude-sonnet-5-5`) and its
  estimated cost.
- **Setup** shows each role's permissions and write policy, harness mode and
  agent.
- **Usage** counts these snapshots as left out, because they report
  occupancy, not billable usage.

Garuda never stores Codex's or Claude's own messages or tool calls, only their
counts. To keep that trail for one run:

```bash
garuda run --role claude-reviewer --no-edits --trajectory trail.jsonl -t "In one sentence, what does client.py do?"
```

## 7. Inspect and recover

Every `garuda flow run` ends with a line such as
`{"flow_session": "…", "state": "completed"}`, or `"state": "stopped"` with a
stop `code`. Use that ID:

```bash
garuda flow show FLOW_ID
garuda flow resume FLOW_ID
```

A resume continues after the last finished step and never runs a step again. A
reviewed step counts as finished only when its review approved it. Otherwise
the resume stops with `flow.review_unresolved`, and you start a new flow.

## What to watch for

- **No-edits is a guardrail, not confinement.** The `read-only` mode and
  instructions above keep a Codex or Claude step from writing. For
  confinement, an external role with `permissions: readonly` needs a
  [Docker image](teams.md#keep-a-role-from-changing-anything).
- **Asking modes need someone to answer.** In a flow or headless run, every
  request is denied. Codex then ends its turn, and the step fails with the
  denied action named.
- **Linux only:** acceptance checks (`--check`), starting flows from the
  dashboard, background runs and worktree isolation.
- **Not available with subscriptions alone:** recipes run native agents, and
  consults are started by native roles. Both need a configured native model.
