# Level 1 · Explore safely

<span class="gd-level">Level 1</span> These recipes ask Garuda only to read.
Read-only runs block Garuda's file-writing tools and allow only inspection
commands such as `ls`, `cat`, `grep`, `rg`, `head`, and `tree`.

!!! info "Read-only is a guardrail, not a sandbox"
    Read-only runs still execute on your machine and can read files your user
    can read. They can't run interpreters, build tools, test runners, `find`,
    or `git`. They also have [known gaps](../guides/safety-and-workspaces.md#read-only-mode-limits):
    MCP tools aren't held to read-only, and environment variables can be
    printed. Check `git status` afterwards, add
    [`--no-edits`](#make-sure-a-run-changes-nothing) to have Garuda check for
    you, and use a [Docker workspace](change-code.md#run-untrusted-code-in-docker)
    for code you don't trust.

## Ask questions about a codebase

<p class="gd-facts">Read-only · Runs on: your machine · Needs: a model key</p>

**Use it when** you open an unfamiliar repository and want a map before reading
code yourself.

```bash
cd /path/to/project
garuda run --mode readonly -t "Explain how this project is organized and where the entry points are"
```

**What happens**

- The agent lists directories, reads files, and searches with `grep` or `rg`.
- It answers in your terminal and saves the run as a session.

??? example "More questions to try"

    ```bash
    garuda run --mode readonly -t "Where is authentication handled? List the files and functions involved"
    garuda run --mode readonly -t "Trace what happens when a user submits the signup form"
    garuda run --mode readonly -t "Which modules have no tests? Give file paths"
    garuda run --agent explore -t "Find every place that reads the DATABASE_URL setting"
    ```

    `--agent explore` is a fast, read-only search profile with a smaller turn
    budget, good for "find where…" questions.

## Make sure a run changes nothing

<p class="gd-facts">Read-only · Checked afterwards · Works with external harnesses too</p>

**Use it when** you need to know, not hope, that a run left the project
exactly as it was.

```bash
garuda run --no-edits -t "Explain how errors are reported to the user"
```

```text
[garuda] no-edits: no changes detected (guardrail, not confinement)
```

**What happens**

- Requests to edit files or run commands that change things are refused. A
  native run gets the `readonly` ceiling; an external harness's approval
  requests are denied and recorded.
- Before the run, Garuda records every file (ignored ones too) with its mode
  and change time, plus the Git refs, `HEAD`, hooks, config and index. After
  the run and everything it started have exited, it compares again.
- If anything changed, or the comparison couldn't finish, you see **changes
  detected**: the answer is withheld, the exit status is `3`, and the changed
  paths are recorded. Nothing is reverted, so you can inspect them.

`--no-edits` checks the workspace in place, so it can't be combined with
`--isolation`. More in
[Safety → no-edits runs](../guides/safety-and-workspaces.md#no-edits-runs-a-guardrail-not-confinement).

## Review your uncommitted changes

<p class="gd-facts">Read-only · Runs on: your machine · Needs: Git</p>

**Use it when** you want a second pair of eyes before committing.

Read-only runs can't call `git`, so save the diff to a file first:

```bash
git diff HEAD > review.diff
garuda run --agent reviewer --mode readonly -t "Review the changes in review.diff. For each problem give the file, the line, and a suggested fix"
rm review.diff
```

**What happens**

- The built-in `reviewer` profile reads the diff and the files it touches.
- You get a list of findings. Its built-in tools can't edit files.

!!! tip
    Ask for a format you can act on, such as "group findings by severity" or
    "only report bugs, not style".

## Plan a change before making it

<p class="gd-facts">Read-only · Runs on: your machine · Needs: a model key</p>

**Use it when** the change is big enough that you want to agree on an approach
first.

```bash
garuda run --agent plan -t "Plan how to add rate limiting to the public API. List the files to change and the tests to add"
```

**What happens**

- The `plan` profile reads and inspects but its rules deny file edits.
- The result is a step-by-step plan saved in the session.

**Next:** carry the plan into an implementation run with
`garuda run --resume latest -t "Implement the plan"`. See
[Level 2](change-code.md#fix-a-bug-with-a-git-safety-net).

## Ask a follow-up question

<p class="gd-facts">Read-only · Needs: an earlier session</p>

**Use it when** you want to keep going without repeating the context.

```bash
garuda sessions
garuda run --mode readonly --resume latest -t "Now explain how errors are reported to the user"
```

**What happens**

- `garuda sessions` lists this project's recent sessions with an ID prefix,
  name, state, queue position, and task.
- `--resume` seeds the new run with the earlier conversation. It accepts
  `latest` (this project's newest), a session name, a full ID, or a unique
  prefix such as `--resume 3f2a`. Add `--all-projects` to take the newest
  session from any project.
- Garuda saves a new session linked to the old one; nothing is overwritten.

To see everything about one session (its state, verification, queue,
approvals, and flow), run `garuda sessions show latest`.

## Read PDFs and spreadsheets

<p class="gd-facts">Read-only · Needs: <code>pip install -e ".[docs]"</code></p>

**Use it when** the answer lives in a document rather than in code.

```bash
garuda run --mode readonly -t "Summarize docs/design.pdf and list its open questions"
garuda run --mode readonly -t "In data/sales.xlsx, which region had the biggest drop month over month?"
```

**What happens**

- The agent uses its PDF and spreadsheet readers instead of dumping the whole
  file into the prompt.

## Browse past runs in the dashboard

<p class="gd-facts">History only · Opens: a local web page</p>

**Use it when** you want to see a run's turns, tool calls, diffs, and metrics
visually.

```bash
garuda web --read-only
```

**What happens**

- A local dashboard opens in your browser. It binds to loopback only and is
  protected by a generated capability token.
- History-only mode can't start conversations, hand off sessions, or run
  recovery.

Add `--no-browser` to skip opening a browser, or `--sessions-dir DIR` to read
another session store. To talk to the agent from the browser, see
[Level 2](change-code.md#work-with-the-agent-in-your-browser).

---

**Ready to let Garuda change code?** Continue to
[Level 2 · Change code](change-code.md).
