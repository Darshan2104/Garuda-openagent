# Troubleshooting

Click a problem to see the fix.

## Install and models

??? question "What is wrong with my setup?"

    Run `garuda doctor`. Every line carries a stable code and a fix, for
    example `harness.logged_out` (log in with the harness's own CLI),
    `harness.cli_missing` (install it), `config.invalid` (with the path of the
    bad value) or `config.project_untrusted` (review the project file with
    `garuda config trust`). A login check that timed out, failed or answered
    something unexpected is reported as such, never as "logged out". Only
    the harnesses your roles use are checked; add `--runtime ID` for another.

??? question "`garuda: command not found`"

    Activate the virtual environment you installed into
    (`source .venv/bin/activate`), or put its `bin` directory on `PATH`. Check
    the install from the same environment:

    ```bash
    python -m pip show garuda-openagent
    ```

??? question "Missing credential or provider authentication error"

    The key must match the selected model. The default model needs
    `OPENROUTER_API_KEY`; another provider needs its own variable. Check which
    model was selected on the `[garuda] reasoning=…` line at startup.

    Garuda never turns a vendor CLI login (Claude Code, Codex, …) into an API key
    for the native runtime. To use a subscription, run that harness through
    [ACP](../use-cases/advanced.md#run-a-task-with-claude-code-codex-or-another-harness).

??? question "The wrong model is being used"

    `--model` on the command line wins. After it come `GARUDA_REASONING_MODEL`,
    `GARUDA_MODEL`, then model bindings in settings. A stray
    `GARUDA_REASONING_MODEL` in your shell beats `GARUDA_MODEL`. Read the
    `[garuda] reasoning=…` startup line to confirm the model and where its
    setting came from. The full order is in
    [Configuration → Models](../guides/configuration.md#models).

??? question "Rate limits, quota, or unexpected cost"

    - Start with a small task in the default mode. `eval` and `rigorous` make
      extra model calls.
    - Bound runs with `--max-turns` and `--deadline-sec`.
    - Leave the collection model off unless you need it (`--no-collection`).
    - Your provider's billing page is the authority on cost.

## Permissions and modes

??? question "\"Approval required but no handler configured\" or \"User denied\""

    The action matched an "ask" rule (for example `sudo`, `chmod`, recursive
    `rm`, or a profile's `ask` path). `garuda run` has nobody to ask, so it
    denies the action and records it. Either:

    - run the task in `garuda chat` with a `smart` profile (such as the default
      `build`) and answer the `y/N` prompt; or
    - change the profile's `path_rules` or `bash_rules` if the action should be
      allowed. See [your own agents](../use-cases/customize.md#create-your-own-agent).

??? question "A read-only run can't run tests, `git`, or `find`"

    That is expected. Read-only mode allows only inspection commands such as
    `ls`, `cat`, `grep`, `rg`, `head`, `tail`, `tree`, `diff`, and `jq`.
    Interpreters, build tools, test runners, `git`, and `find` are denied.

    - To review changes, save the diff first: `git diff HEAD > review.diff`.
    - To run tests, drop `--mode readonly` and use a
      [Docker workspace](../use-cases/change-code.md#run-untrusted-code-in-docker)
      if you don't trust the code.

??? question "Project Python tools or hooks don't load"

    They execute repository code, so a repository can't enable them by itself.
    Pass `--load-project-tools` for one run, or set `load_project_tools: true`
    (and `trust_project_hooks: true` for hooks) in `~/.agent/settings.yaml`.

## Workspaces

??? question "The `sandbox` workspace refuses to start"

    No OS sandbox backend is available. On Linux, Bubblewrap must be installed,
    and user namespaces must be allowed by the host's security settings. On
    macOS, Seatbelt is used.

    Use a Docker workspace instead. `--allow-unsandboxed` runs the task
    **unconfined** on the host; use it only when that is acceptable.

??? question "The Docker workspace fails or can't install dependencies"

    - Make sure the Docker daemon is running: `docker info`.
    - `--no-network` means the container can't download anything. Use an image
      that already has your toolchain (`--docker-image python:3.12`), or drop
      `--no-network`.
    - Raise the limits with `--docker-memory 4g --docker-cpus 4` if the build
      runs out of memory.
    - For `remote`, the workspace path must exist on the remote Docker host.

??? question "Garuda refuses because the workspace is in use"

    Another session holds a lease that lets it edit this workspace, and Garuda
    won't let two sessions mix their changes. If that session is still running,
    wait for it, or start yours in its own worktree:

    ```bash
    garuda run --isolation worktree -t "…"
    ```

    If the message says the holder's **parent is dead but descendant cleanup is
    not proved**, the other session crashed (for example its terminal was
    closed) and Garuda can't prove the commands it started have stopped. That
    lease stays held: `garuda doctor` lists it as `lease.stale`, and
    `garuda runtime recover` and `reclaim` refuse while it's there. There is no
    command yet that releases it, so keep working with `--isolation worktree`
    (or `--no-edits` for read-only work). Don't delete lease records by hand.

??? question "`sessions merge` fails with `integration.check_failed`"

    The check runs in Docker with your merged code mounted read-only, **no
    network**, and a non-root user, using `python:3.12-slim` unless you pass
    `--image`. That image has no pytest, and it can't install anything. Build
    an image with your test tools and pass it:

    ```bash
    printf 'FROM python:3.12-slim\nRUN pip install --no-cache-dir pytest\n' | docker build -t my-checks -
    garuda sessions merge NAME --image my-checks --check "python -m pytest -q -p no:cacheprovider"
    ```

    `-p no:cacheprovider` stops pytest from trying to write its cache to the
    read-only mount. Other codes: `integration.no_confined_checker` (Docker
    isn't available; a host check is never used instead), and a merge conflict
    refuses before any check runs.

??? question "A `--no-edits` run exits with status 3 and withholds its answer"

    Garuda found a change in the workspace after the run (or couldn't finish
    comparing), so it reports **changes detected** and records the changed
    paths. Nothing was reverted: run `git status` to inspect them. `--no-edits`
    can't be combined with `--isolation`, because it checks the workspace in
    place.

## Sessions and runtimes

??? question "`--resume` or `--session` says the reference is ambiguous or missing"

    Use `garuda sessions` and pass a longer prefix, the full ID, or `latest`.
    Ambiguous references fail before anything changes.

??? question "Native resume refuses: the session is owned by an external runtime"

    The session was handed off and the harness may still be acting. Check, take
    it back once the harness has stopped, then continue:

    ```bash
    garuda runtime recover --session latest
    garuda runtime reclaim --session latest
    garuda runtime resume --session latest -t "Continue natively"
    ```

??? question "The Approvals page says `stale_request`, `already_answered` or `expired`"

    The page answered a request that is no longer the one it showed
    (`stale_request`: reload), one that already has an answer or a decision from
    the terminal, another tab or an earlier click (`already_answered`), or one
    past its expiry (`expired`; it is denied). "Answer recorded" only means the
    broker has the file: if the permission ceiling changed meanwhile, the broker
    denies it and says why in the session's approval decision.

??? question "A `--bg` session stays `queued`"

    It's waiting for a free slot: its runtime is at its `capacity` limit, which
    foreground runs share. `garuda sessions` shows what holds the slots. A
    session that reads `crashed` lost its worker; its slot stays held until
    Garuda can prove its child processes are gone, so it isn't silently
    replaced. `garuda sessions cancel SESSION` removes a queued session or stops
    a running one. The worker's output is in
    `~/.agent/sessions/<id>/worker.log` (cut at 1 MB).

??? question "A run is refused: `runtime 'native' is at its capacity of N`"

    A foreground run never waits for a slot. Wait for a running session to
    finish, start this one with `--bg` so it queues, or raise `capacity` in
    `~/.agent/settings.yaml`.

??? question "A background run says its approvals were denied"

    Background runs, like every `garuda run`, have nobody to answer an approval,
    so the request is denied as soon as it's asked. Run the task in
    `garuda chat` to approve actions yourself, or change the agent's rules if
    the action should be allowed.

??? question "`sessions cancel` says the worker's pid was reused"

    The recorded process identity no longer matches that pid, so nothing was
    signalled; the session is marked failed. Garuda never signals a process it
    cannot prove is its worker.

??? question "A runtime shows as unavailable"

    Run `garuda runtime inspect <id>`. Common causes: the vendor CLI isn't
    installed or isn't on `PATH`, the runtime is disabled in
    `~/.agent/settings.yaml`, or its version check fails. Login and quota often
    show as `unknown`; that alone does not make a runtime unavailable.

??? question "A `consult` call is refused (`consult.*`)"

    The code says why, and a refused consult never runs the other role or
    spends its budget (except `consult.timeout`, `consult.failed` and
    `consult.unexpected_changes`, which did run and say so in the receipt).

    - `consult.not_granted`: the asking role's `consult:` list (user
      `garuda.yaml`) does not include the target. Projects cannot grant it.
    - `consult.nested`: a consulted role tried to consult.
    - `consult.limit`, `consult.too_long`: `consults.max_per_session` or the
      question size limit was reached; raise it in your user file.
    - `consult.busy`: another consult from this session (or the same request id)
      is still in progress; consults run one at a time per asker.
    - `consult.capacity_unavailable`: the target's harness is at its
      `max_parallel`; consults never wait for a slot.
    - `consult.snapshot_unsupported`, `consult.snapshot_unstable`: the workspace
      is not a git checkout Garuda can snapshot, or it changed (or background
      processes ran, or pausing the asker failed) while the snapshot was
      taken. The unlaunched request's slot is returned; retry when it is quiet.
    - `consult.target_unavailable`: the target role's agent requires a
      final-output schema or a completion check, which a read-only question
      can't satisfy.
    - `consult.isolation_unavailable`: an external target needs the Docker
      read-only confinement and it is not available.
    - `consult.transport_unsupported`: an ACP role asked to consult, but its
      adapter version has not proved MCP forwarding, structured permission
      identity and a workspace-pause handshake, so the tool is not offered. Use
      a native role for the asking side.
    - `consult.unauthenticated`: the consult endpoint of an ACP session refused
      the token (old or wrong); it is replaced whenever the session resumes.
    - `consult.payload_changed`: the same request id was reused with a different
      question.
    - `consult.interrupted`, `consult.quarantined`: Garuda was killed mid-consult,
      or the consulted process could not be confirmed stopped. The slot stays held
      until you inspect it with `garuda sessions`.

## Agents, roles and flows

??? question "`garuda agent check` reports `agent.*` errors"

    Version 1 definitions are strict, so mistakes are caught before a run. The
    message names the field and a fix. The common ones:

    - `agent.unknown_field`: a typo such as `limits.max_turn`; `garuda agent
      show garuda/build` lists the real fields.
    - `agent.unsupported_field`: a field that isn't supported yet, such as
      `hooks:` (use settings hooks) or `write_policy:` (set it on a role).
    - `agent.invalid_budget`: the context numbers don't add up, or a deadline
      isn't positive.
    - `agent.project_widening`, `agent.required_gate`: a project agent asked for
      more permission than `agents.project_ceiling` allows, or tried to turn off
      a required check.
    - `agent.output_schema_*`: the output schema uses an unsupported keyword or
      a reference outside the file.

    See the [Agent definitions guide](../guides/agents.md#checks-before-a-run).

??? question "A run says `verification: unavailable`"

    No check ran, so nothing verified the result. Pass `--check COMMAND`, add
    `checks:` to your `garuda.yaml`, or trust the project's checks with
    `garuda config trust`. The agent's own completion gate is reported
    separately as its self-check.

??? question "`config.project_untrusted`: the project's checks are ignored"

    The project's `garuda.yaml` asks Garuda to run something (checks, or a
    `native` model), and you haven't trusted this exact file. Review and trust
    it at a terminal with `garuda config trust`. Any edit to the file needs
    trust again.

??? question "`garuda doctor` reports `config.ok` but a role never runs"

    Check the role's `harness`: it must be a runtime ID from
    `garuda runtime list` (`native`, `claude`, `codex`, `cursor`, `opencode`,
    `pi`, `goose`). `doctor` only checks the harnesses it recognises, so a
    misspelled one is skipped rather than reported.

??? question "A flow stops with `flow.output_missing`"

    The step didn't end with the `<garuda-artifact type="…">` block the next
    step needs. For a native role, the block must be in the summary it passes to
    `task_complete`. Give the role an agent that says so; see
    [Level 5](../use-cases/teams.md#run-a-plan-build-review-flow).

??? question "A read-only flow step runs until it hits its turn limit"

    The native completion checker can ask a read-only step for evidence it
    isn't allowed to collect. Give read-only roles an agent with
    `completion: {verifier: false}`, as shown in
    [Level 5](../use-cases/teams.md#run-a-plan-build-review-flow).

??? question "A flow stops with `flow.review_not_independent`"

    The reviewer would run as the same harness and model as the role it
    reviews, one of that role's fallbacks, or a role it may consult. Give the
    reviewer a different model, or remove the reviewer from the reviewed role's
    `consult:` list.

??? question "`garuda flow show` says no such file"

    `flow show` and `flow resume` need the full flow ID that `garuda flow run`
    prints at the end (`"flow_session": "…"`), not a prefix or a name.

??? question "A read-only external role refuses with `workspace.readonly_unenforced`"

    A role on an external harness with `permissions: readonly` runs only in a
    Docker container that Garuda proves is confined. Set
    `harnesses.<id>.confinement.image` in your `garuda.yaml` to an image where
    you installed and logged in to the harness, and make sure Docker is
    running. Garuda never runs such a role on the host instead.

## Dashboard and MCP

??? question "The dashboard port is busy, or the browser doesn't open"

    Pick another port with `--port 8790`. Use `--no-browser` on headless
    machines and open the printed address yourself.

??? question "MCP tools don't appear"

    Run `garuda mcp list --no-connect` to see which config files were found
    and which servers they define. Then `garuda mcp list` connects and lists
    each server's tools. A project server marked "not trusted" never starts;
    review it and run `garuda mcp trust`. Project and global configs merge by
    default; set `GARUDA_MCP_MERGE=0` to use only one, or pass `--mcp-config FILE`.

??? question "`session.project_key_missing`: every run refuses"

    The key that names your projects (`.identity/key` in the session store) is
    gone, but saved sessions already use it. Garuda won't make a new one on its
    own, because that would split every project in two. Stop any running
    sessions, then run `garuda doctor --recover-project-ids`. Retained workspace
    leases block recovery even after expiry or parent death, because descendant
    cleanup remains unproved; the key is not staged or replaced on refusal.
    Sessions whose
    repository is still where it was (same path and filesystem identity) move
    to the new key with their names; any others keep their old id and still
    resolve by full ID.

## Still stuck?

Generate a redacted support bundle and attach it to an
[issue](https://github.com/Darshan2104/Garuda-openagent/issues):

```bash
garuda runtime support --session latest
```
