# Level 6 · Automate

<span class="gd-level">Level 6</span> Run Garuda from files, shell scripts, CI
jobs, Python programs, and HTTP clients.

| Approach | Best for | Starts with |
|---|---|---|
| Recipe | A fixed sequence of steps with parameters | `garuda recipe run file.yaml` |
| Shell or CI | One task with an exit status and JSON output | `garuda run --json` |
| Background queue | Many tasks started from a script, polled later | `garuda run --bg`, `garuda sessions show SESSION --json` |
| Quota recovery | Continue eligible Codex flow steps after quota returns | `garuda recovery enable` |
| Python SDK | Embedding the agent in your own program | `SoftwareAgent` |
| HTTP service | Submitting and polling jobs from another process | `garuda serve` |

## Run a multi-step recipe

<p class="gd-facts">Changes files: depends on the steps · Format: YAML</p>

**Use it when** you run the same plan → build → test sequence often.

Save this as `fix-and-test.yaml`:

```yaml
name: fix-and-test
description: Analyze an issue, implement a fix, and run tests
parameters:
  - name: issue
    type: string
    required: true
  - name: test_command
    type: string
    default: "pytest"
steps:
  - agent: plan
    prompt: "Analyze this issue and propose a fix plan: {{issue}}"
  - agent: build
    prompt: "Implement the fix for: {{issue}}"
  - agent: build
    prompt: "Run {{test_command}} and fix any failures related to: {{issue}}"
```

Run it:

```bash
garuda recipe run fix-and-test.yaml --param issue="Login fails when the email has uppercase letters" --param test_command="pytest -q"
```

**What happens**

- Steps run in order. Each step's output is appended to the next step's prompt
  under "Prior step output".
- `{{name}}` placeholders are filled from `--param KEY=VALUE`, then from
  defaults. A missing required parameter stops the recipe with an error before
  any step runs.
- Recipe steps aren't saved as individual sessions. The recipe holds and
  heartbeats one workspace lease across the whole sequence, including MCP/tool
  setup and cleanup. Compatible readers can coexist; a conflicting writer is
  refused by the existing lease owner.
- Each step can set `agent` (a profile) and `mode`.
- The recipe stops at the first failed step and tells you how many later
  steps did not run.

[Garuda Starters](workflows.md) is the simpler entry for the five packaged
role/flow journeys. Recipes keep custom prompt sequences; they do not gain typed
flow artifacts, independent review or a new session/resume engine.

## Use Garuda in scripts and CI

<p class="gd-facts">Exit status: 0 when the run succeeds, non-zero otherwise</p>

**Use it when** another program decides what to do with the result.

=== "Shell script"

    ```bash
    if garuda run --mode eval --deadline-sec 1800 -t "Make the test suite pass"; then
      echo "Garuda accepted the result"
    else
      echo "Garuda could not finish" >&2
    fi
    ```

=== "JSON events"

    ```bash
    garuda run --json --mode readonly -t "List the public functions in src/api" > events.jsonl
    jq -r 'select(.type == "tool_call") | .payload.name' events.jsonl
    jq 'select(.type == "session_end") | .payload' events.jsonl
    ```

    `--json` prints one JSON object per line with `type`, `timestamp`,
    `session_id`, and `payload`. Types include `session_start`, `tool_call`,
    `tool_result`, `model_response`, `verification`, and `session_end`.

=== "GitHub Actions"

    ```yaml
    # .github/workflows/garuda-review.yml
    name: Garuda review
    on: pull_request
    jobs:
      review:
        runs-on: ubuntu-latest
        steps:
          # Check out the trusted base branch, so the pull request's own
          # .agent/ files and instructions never run. Its diff is only data.
          - uses: actions/checkout@v4
            with:
              ref: ${{ github.event.pull_request.base.sha }}
              fetch-depth: 0
          - uses: actions/setup-python@v5
            with:
              python-version: "3.12"
          - run: pip install git+https://github.com/Darshan2104/Garuda-openagent.git
          - run: |
              git fetch origin "pull/${{ github.event.pull_request.number }}/head"
              git diff HEAD...FETCH_HEAD > review.diff
          - run: garuda run --agent reviewer --mode readonly -t "Review the changes in review.diff and list concrete problems"
            env:
              OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
    ```

    The diff reaches the model, and read-only mode is a guardrail with
    [known gaps](../guides/safety-and-workspaces.md#read-only-mode-limits).
    GitHub masks the secret in logs, and pull requests from forks don't receive
    it. Run this only on changes you are willing to send to your provider.

=== "Background and poll"

    ```bash
    id=$(garuda run --bg --check "python -m pytest -q" -t "Fix the failing tests")
    while garuda sessions show "$id" --json | jq -e '.label | IN("queued", "working", "waiting")' >/dev/null; do
      sleep 10
    done
    garuda sessions show "$id" --json | jq '{label, verification, artifacts}'
    ```

    `--bg` prints only the session ID on stdout. `sessions show SESSION --json`
    returns the same record the dashboard uses: `label`, the four-part
    `state`, `verification`, `self_check`, `queue`, `branch`, `approvals`,
    `flow`, `consults` and the bounded `artifacts` projection. `garuda sessions
    --json` lists every session.

    Inspect the retained output contract without printing its payload:

    ```bash
    garuda sessions show "$id" --json | jq '.artifacts.references[] | {owner, source, completeness, omitted_source_bytes, availability, retention}'
    ```

    `complete` means the identified source representation was captured in full.
    `partial` covers sampled or clipped observations, including a running
    background tail. Background completion requires the capture owner's EOF
    receipt and process-group settlement; launcher exit alone leaves a task
    incomplete and available for further polling. Unsafe or replaced logs are
    unavailable and remain registered for reconciliation, even while the command
    is running. `failed` preserves a capture failure. An otherwise known
    reference becomes `unavailable` when its payload is pruned, missing, changed
    or unsafe to open. For a delivered inline result, `omitted_view_bytes`
    describes bytes left out of its retained message. Stored-artifact descriptors
    describe their stored representation, rather than every later preview.
    The dashboard session page shows the same states. The 14-day buffer policy is best effort, not a retrieval deadline.
    Older `.txt` buffers have `unknown` completeness and producer provenance;
    their current size and digest are labelled `observed` rather than invented
    as historical facts. Older version-1 typed child references already contain
    size/digest commitments: inspection and resume preserve those as `recorded`,
    refuse mismatching bytes, and leave unrecorded historical facts unknown.
    Inspection shares its source allowance across metadata,
    payloads and failed reads; exhausted verification remains unknown unless
    the retention owner has already confirmed pruning. That known unavailability
    survives skipped verification; missing bytes alone do not prove pruning. Omitted
    reference counts can be lower bounds when coverage is incomplete. Malformed
    flow metadata leaves declaration coverage unknown in CLI/API detail while valid
    retained buffers and failure causes remain visible. Invalid summary runtime
    identities are withheld locally rather than hiding independent artifacts.
    Malformed nested flow receipts are skipped locally; valid sibling attempts,
    buffers and causes remain visible in CLI/API detail and consult discovery.
    `.flow.coverage.receipts` becomes `unknown`, separately from declaration and
    journal coverage; it does not claim receipt integrity or verification. Unsafe
    metadata identities are withheld with per-field coverage, independently of
    payload redaction. Unsafe capture sidecar paths refuse buffer reads and
    inspection instead of falling back to legacy bytes, including when directory
    discovery clips before their exact sidecars. Exhausted sidecar checks leave
    availability unknown. A valid sidecar cannot replace a conflicting retained
    size/digest commitment. Unknown sidecar or repeated transcript fields preserve
    known producer, size and digest facts; resumed ordinary reads enforce those
    inherited commitments. Inspection refuses conflicting references, and resume
    refuses conflicting inherited records. Long provider IDs remain retrievable through their
    supported IDs and canonical discovered aliases. Cached reads preserve capture
    loss; archiving an already clipped inline result cannot restore its missing
    bytes. Malformed records or uninspected staging entries make coverage unknown
    rather than inventing artifact counts. Failure evidence reserves its final
    metadata recheck within the existing aggregate read limit, so exhausted
    optional artifact reads retain the original cause with unknown artifact
    coverage. Changed metadata still refuses the failure projection. Changed
    session/store directory bindings also refuse; regular directory replacement
    cannot redirect the projection into another project's payloads. Inline
    references become unavailable when their retained text is missing or no longer
    matches its recorded digest.
    With buffering disabled, pruning still retains known capture facts, while
    discarded inline bytes are unavailable with unknown integrity and view loss.
    Literal buffer-looking text does not change an inline capture's owner.
    Character-slice frames and receipt errors keep their exact bytes and budgets
    while retaining inline capture facts; embedded buffer-looking text stays text.
    Post-tool hooks cannot replace frame bytes, offsets or receipt-error contracts,
    even by recomputing hashes. Changed deliveries produce bounded failure feedback.
    Runner-generated lookup, argument and exception feedback also carries inline
    facts before hooks or storage. Its text cannot grant a collection worker a
    parent buffer; unretained earlier capture/loss facts stay unknown.
    Permission/hook refusals and native argument-parse feedback use the same
    bounded inline contract, so echoed names cannot widen collection access.
    Brief handoffs and collection workers use the same owner-aware discovery,
    so known inline or failed output cannot grant a parent buffer by mentioning it.
    Post-tool hooks may format a child summary within its delivery budget, but
    validated child output, delivery, schema and producer identity remain those
    captured before the hooks. Changing those facts makes delivery unavailable.
    A rejected or null capture contract grants no buffers; text-only compatibility
    applies to legacy messages with no capture contract.
    Unknown retrieval bindings preserve historical completeness and omissions;
    those facts do not establish current availability or grant access.
    Pruned buffer pointers explicitly leave current availability unverified;
    inspection and retrieval perform their existing owner checks.
    Pruning a typed child handoff retains its canonical result contract for resume.
    Native `garuda run --resume "$id" "Continue the investigation"` keeps the
    original producer identity while rebinding copied captures to the new storage
    session. Source selection uses the retained no-follow metadata reader before
    workspace or lease setup. Names, prefixes and latest remain project-scoped;
    explicit full ids and all-projects retain their existing meanings. A replacement
    before the later binding cannot bypass project authorization, and incomplete
    metadata budgets refuse selection. The source/store directory binding stays held through
    recovery, replay, discovery and copying. Cancellation waits for recovery to
    finish before releasing that source binding and workspace lease; replacements refuse before foreign
    payload reads. Child buffer-publication failures remain failed/unavailable
    facts with unknown payload size/digest, including event-only evidence, rather
    than disappearing or becoming retrieval references. Captures bind to the new
    session. Confirmed retention pruning survives older transcript availability
    during resume and exact-sidecar lookup after clipped discovery, including large
    captures and exhausted verification budgets. Unconfirmed loss remains unknown
    when unverified. Partial, failed and pruned facts survive, including event-only facts;
    copied corrupt payloads still fail their recorded integrity checks. Resume
    refuses malformed capture records and symlinked transcript/artifact inputs
    before a model prompt. If commitment discovery is incomplete, every copied
    payload needs a retained size/digest commitment; an unchecked legacy fallback
    refuses startup. Missing inheritance on a resumed source remains unknown
    across later resumes. Large regular saved transcripts remain replayable,
    while their bounded inspection can stay unknown. Bounded inherited facts can retain unknown coverage, and
    referenced child results keep their original producer and byte commitments.

Add `--trajectory run.jsonl` to also save the events to a file after the run.

!!! tip "JSON from the other commands"
    `garuda doctor --json`, `garuda agent show NAME --json`,
    `garuda agent check NAME --json`, `garuda memory list --json` and
    `garuda runtime list --json` are all meant for scripts. `doctor` and
    `agent check` exit `1` when they find an error.

## Continue quota-stopped Codex flows

<p class="gd-facts">Changes user crontab: enable/disable · Schedule: minute 27 each hour · Output: JSON</p>

**Use it when** a sequential CLI flow uses Codex and should continue after a
proved subscription-quota reset even when the original Garuda process has
exited.

Enable the user-owned schedule once, before starting the flows you want it to
cover:

```bash
garuda recovery enable
garuda flow run plan-build-review -t "Add bounded retry handling"
```

`enable` installs one marked user-cron entry for `27 * * * *`. It preserves
unrelated entries and uses the current Python environment's absolute entry
point and the absolute Codex executable found at installation. Keep that Python
environment available; if you move or remove it, reinstall Garuda and run
`enable` again. Cron runs only while the host is awake and does not catch up a
check missed during sleep.

Only eligible sequential CLI flows started after recovery was enabled are
enrolled. Enabling it does not scan or adopt old half-finished flows. A Codex
step must be safely closed and have a quota-shaped failure; permission denials
and generic failures do not become quota retries. If the official quota read is
missing or temporarily unavailable, the eligible stop remains waiting with that
reason—it does not claim exhaustion or dispatch work.

Inspect the schedule and all saved continuations:

```bash
garuda recovery status
garuda flow show FLOW_ID
```

`recovery status` prints JSON. `flow show` and the dashboard's flow detail show
the recovery state, reason, and next check. In the schedule object, `enabled`
is persisted operator configuration, while `installed` validates the exact cron
schedule and command for this private root and its owner-only executable regular
wrapper. If installation is missing or mismatched, `reason` explains why;
`reason` is null when installation is verified. Status does not run the wrapper
or check whether its interpreter and packages remain usable.

Enablement is published only after successful installation readback. If
`enable` fails with an OS error such as `Operation not permitted`, recovery
stays disabled. Resolve the reported user-crontab error before retrying
`garuda recovery enable`, then inspect `garuda recovery status`. An existing
private watcher for old flows does not enroll future flows in built-in recovery.

You can also run a check immediately:

```bash
garuda recovery tick
```

Each tick uses the official Codex read-only account/rate-limit interface. The
automatic path is limited to the verified Codex CLI versions `0.159.3` and
`0.163.0`; it requires the enrolled account and version to match, ordinary use
to be affirmatively allowed, and every reported window—including a weekly
window—to be below exhaustion. Evidence that cannot affirm availability never
launches work. A missing or temporarily unavailable quota observation leaves
the continuation `waiting-for-quota` with its reason and next check; later ticks
try again. A changed account/version identity, configuration or source identity,
workspace, or receipt blocks the continuation instead. A tick dispatches at
most one eligible flow; later flows wait for later ticks.

Quota is checked again after the dispatch claim and before the resumed step. If
another process consumes the available quota in that gap, the flow returns to
`waiting-for-quota`; it is not treated as a permanent failure or replayed.

Continuation keeps existing workspace edits and completed step receipts. A
quota-stopped reviewer continues as the pending reviewer without rerunning its
completed coder, and the existing review-round budget is preserved. Garuda
uses saved flow and workspace context; it does not promise restoration of the
terminal's exact model state and never replays a transcript or completed shell
actions. If dispatch ownership is lost, the continuation becomes `uncertain`
and is not replayed automatically.

Cancel one continuation that has not dispatched, or disable future automatic
checks:

```bash
garuda recovery cancel FLOW_ID
# The shared session cancellation path reaches the same pending continuation:
garuda sessions cancel FLOW_ID
garuda recovery disable
```

`cancel` revokes that pending continuation and refuses one already dispatched;
the shared session Stop/cancel action uses the same rule. Neither is a stop for
an already dispatched flow. `disable` first disables automatic dispatch and
then removes only Garuda's marked cron block. It does not cancel or signal a
live flow.

This release does not recover standalone `chat` or `run` sessions,
check-protected or dashboard-owned flows, parallel groups, or Claude quota
stops. When recovery is enabled, an owned/check-protected/dashboard flow or a
parallel flow projects an `unsupported` recovery state and reason in flow
status instead of appearing enrolled.

## Call Garuda from Python

<p class="gd-facts">Async API · Runs the same native loop as the CLI</p>

**Use it when** Garuda is one part of a larger Python program.

=== "One task"

    ```python
    import asyncio

    from garuda.sdk import SoftwareAgent


    async def main():
        agent = SoftwareAgent(workspace=".", agent="build", mode="readonly")
        result = await agent.run("Summarize this repository")
        print("success:", result.success, "turns:", result.turns)
        print(result.final_message)


    asyncio.run(main())
    ```

=== "Conversation"

    ```python
    import asyncio

    from garuda.sdk import SoftwareAgent


    async def main():
        conversation = SoftwareAgent(workspace=".").conversation()
        try:
            first = await conversation.run("Find the slowest test in tests/")
            print(first.final_message)
            second = await conversation.run("Now make it faster without changing what it checks")
            print(second.final_message)
        finally:
            await conversation.close()


    asyncio.run(main())
    ```

=== "Your own tool"

    ```python
    import asyncio

    from garuda.sdk import SoftwareAgent
    from word_count import WordCountTool  # the class from Level 3


    async def main():
        agent = SoftwareAgent(workspace=".")
        agent.register_tool(WordCountTool())
        result = await agent.run("How many words are in README.md?")
        print(result.final_message)


    asyncio.run(main())
    ```

=== "An agent definition"

    ```python
    import asyncio

    from garuda import AgentSpec, SoftwareAgent


    async def main():
        spec = AgentSpec.load("api-summary", workspace=".")
        agent = SoftwareAgent(workspace=".", agent=spec.narrow(limits={"max_turns": 12}))
        result = await agent.run("List the public functions in src/calc")
        print(result.output)  # the validated JSON, when the agent has an output schema


    asyncio.run(main())
    ```

    `AgentSpec.load` resolves a name exactly as `garuda run --agent NAME` does.
    `narrow(...)` returns a stricter copy (fewer turns, fewer tools, a tighter
    permission mode); anything looser is refused with `agent.narrow_refused`.
    See [structured JSON output](customize.md#get-structured-json-back).

Custom environments implement `read_bytes(path)` alongside `read_file(path)` and
`write_file(path, content)`; image, PDF and spreadsheet tools use it under the
same backend file-path policy.
For workspace-policy refusals, raise `WorkspaceAccessDenied` from
`garuda.workspace.protocol` so the run records denial evidence. Ordinary OS
`PermissionError` stays a tool error. Environments that omit `workspace_root`
retain the permission engine's configured root.

`SoftwareAgent` also accepts `model=`, `workspace_kind=` (for example
`"docker"`), `docker_image=`, and `runtime=` to run one turn on an
[external harness](advanced.md#run-a-task-with-claude-code-codex-or-another-harness).
`agent.run(task, resume="latest")` continues this workspace's newest saved
session; pass `resume_all_projects=True` for the newest one anywhere.

## Call Garuda over HTTP

<p class="gd-facts">JSON-RPC 2.0 over HTTP POST · Loopback by default · Bearer token required</p>

**Use it when** an editor plugin, service, or another language needs to submit
tasks.

Start the server with a token you choose:

```bash
export GARUDA_SERVE_TOKEN=replace-with-a-long-random-string
garuda serve --workspace . --port 8765
```

If you don't set a token on loopback, Garuda generates one and prints it once.
Binding a non-loopback `--host` without a token is refused.

Submit a job, then poll it:

```bash
curl -s http://127.0.0.1:8765/ -H "Authorization: Bearer $GARUDA_SERVE_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"submit","params":{"task":"Summarize this repository","mode":"readonly"}}'

curl -s http://127.0.0.1:8765/ -H "Authorization: Bearer $GARUDA_SERVE_TOKEN" \
  -d '{"jsonrpc":"2.0","id":2,"method":"status","params":{"job_id":"JOB_ID"}}'

curl -s http://127.0.0.1:8765/ -H "Authorization: Bearer $GARUDA_SERVE_TOKEN" \
  -d '{"jsonrpc":"2.0","id":3,"method":"result","params":{"job_id":"JOB_ID"}}'
```

Replace `JOB_ID` with the `job_id` returned by `submit`.

| Method | Does |
|---|---|
| `health` | Returns `{"status": "ok", "version": …}` |
| `submit` | Starts a job. Params: `task`, plus optional `agent`, `mode`, `workspace`, `workspace_kind`, `model`, `resume` |
| `status` | Job state, turn count, and event count |
| `events` | New events since `cursor`, for streaming progress |
| `result` | `success`, `final_message`, and `turns` once the job is done |
| `cancel` | Stops a job |
| `run` | Runs a task and waits; returns the result and all events |
| `jobs`, `sessions`, `list_agents` | Lists jobs, saved sessions, and profiles |
| `runtime_list`, `runtime_inspect`, `runtime_handoff`, `runtime_recover`, `runtime_support` | Runtime operations; see [Level 7](advanced.md) |

Requests that carry a browser `Origin` header are rejected, so a web page you
visit can't drive the server. `--max-jobs` limits concurrent jobs (default 4).

**Limit what a request can ask for.** A request names an agent; it can never
send a definition. Pin down which names are allowed and how loose their
permissions may be:

```bash
garuda serve --workspace . --agent build --allow-agent build --allow-agent reviewer --permission-ceiling smart
```

- `--allow-agent NAME` (repeatable) lists the agents a request may select;
  anything else is refused with `agent.not_allowed`.
- `--permission-ceiling` is the loosest mode a requested agent may run with
  (default: that of `--agent`), so naming `garuda/harbor` can't raise the
  server's authority.
- With either limit configured, launch settings stay with the operator: requests
  cannot change `workspace`, `workspace_kind`, `docker_host`, `docker_image`,
  `agents_dir`, or `mcp_config`. Repeating the configured value is accepted.
  Choose these settings when starting the server. Without either explicit limit,
  authenticated callers retain control of these launch settings.

---

**Next:** use external harnesses, handoffs, and routing in
[Level 7 · Advanced](advanced.md).

## Read a bounded file page or tail

For deterministic inspection without a model call:

```python
import asyncio
from garuda.tools.files import ReadFileTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment

async def inspect():
    tool = ReadFileTool()
    env = LocalEnvironment(".")
    ctx = ToolContext(session_id="inspection")
    page = await tool.execute(
        {"path": "README.md", "offset": 20, "limit": 10, "bounded": True}, env, ctx)
    tail = await tool.execute(
        {"path": "README.md", "tail": True, "limit": 5}, env, ctx)
    print(page.content, tail.content)

asyncio.run(inspect())
```

Local and sandbox reads decode UTF-8 in 1 MiB blocks and retain at most 2000
lines and 2000 displayed characters per line. Offsets stream through the
preceding bytes without a total-size ceiling, so a late page costs I/O like the
whole-file read it replaces but bounded memory. Tail seeks backwards from EOF in
8 KiB blocks and decodes only the located tail, so its cost follows the tail's
size, not the file's; when that tail starts beyond the first 1 MiB, line numbers
and `total_lines` are unknown and earlier bytes are not UTF-8 validated. Counts
are exact only after a scan from the start reaches EOF; otherwise `total_lines`
is unknown. Concurrent changes are not a file snapshot. Newline handling
retains existing `str.splitlines()` semantics.

Docker and remote backends explicitly do not support bounded text reads yet.
`bounded=True` or `tail=True` refuses there; ordinary reads keep their existing
whole-file compatibility path. Custom environments can implement the optional
`BoundedTextEnvironment` protocol from `garuda.workspace.text_reads`. This
capability must use the backend's existing path policy; it grants no new file
access. Existing `read_file()` callers keep their original contract.
