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
    the harnesses your roles use are checked, including each role's
    `fallback:` harnesses; add `--runtime ID` for another. A fallback-only
    harness that is missing or logged out is a warning, not an error: the
    role can still start, but that fallback can never be taken.

    Doctor also validates `~/.agent/settings.yaml` (or `GARUDA_GLOBAL_SETTINGS`)
    with the runtime's loaders and section validators: capacity, routing, models,
    model bindings, collection, reminders, language servers, hooks, agent ceilings,
    runtime manifests and disablement. It collects errors across sections as
    `settings.invalid`, with the file path, key and a fix hint. Invalid YAML or a
    non-mapping file is also an error. Missing, empty and comment-only files
    use defaults, as do null sections such as `routing:` with all entries
    commented out. Non-null invalid values such as `routing: 7` remain errors.
    Errors exit 1 and appear in `garuda doctor --json`; valid settings exit 0
    unless another check fails. Invalid global settings prevent harness probes;
    configured hooks are never executed by this settings check.

??? question "`harness.logged_out` when I run a role or `--runtime`"

    The harness's own login check (for Codex, `codex login status`) says you
    are logged out, so Garuda refuses the run before it starts (exit 2) and
    never runs it natively instead. Log in with the harness's CLI as the
    message says (for Codex, `codex login`), then run again. A role with a
    `fallback:` list moves to the next entry instead, and refuses with
    `harness.no_candidate` only when no entry can start. A `--bg` run is
    refused when you queue it; if the harness logs out while the run waits,
    `garuda sessions show` reads `refused` with `harness.logged_out`.

??? question "`garuda: command not found`"

    Activate the virtual environment you installed into
    (`source .venv/bin/activate`), or put its `bin` directory on `PATH`. Check
    the install from the same environment:

    ```bash
    python -m pip show garuda-openagent
    ```

??? question "`requires a different Python` or `python3.12: command not found`"

    Garuda needs Python 3.12+. Install it and recreate the virtual environment
    with that interpreter, following [Install](../guides/getting-started.md#1-install).
    After activating the environment, check `python --version`.

??? question "`error: externally-managed-environment`"

    Install into a virtual environment as shown in the
    [Quickstart](../guides/getting-started.md#1-install).
    Don't use `--break-system-packages` to override your system Python's protections.

??? question "Installing on Windows"

    Native Windows is not supported or tested. Use WSL 2 and follow the
    [Linux install steps](../guides/getting-started.md#1-install) inside it.

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
    - `harness.limit_reached: You've hit your usage limit ...` means an
      external harness (for example Codex or Claude Code) refused the turn at
      its plan limit. Wait for the reset time in the message (Claude's is
      given in UTC, for example `resets at 2026-11-26 15:00 UTC`), or use
      another harness in the role's `fallback:` chain. Garuda does not resume
      the run by itself when the limit resets. A host error such as `Disk quota
      exceeded (os error 122)` is not typed as a limit: it stays a plain
      `agent error`, so free disk space instead. Error text an agent sends
      back is redacted for secret patterns before Garuda stores or shows it.

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
    not proved**, the other session crashed (for example it was killed with
    `kill -9`, or its terminal was closed). `garuda doctor` lists the lease as
    `lease.stale`. Release it with:

    ```bash
    garuda runtime recover --session ID   # the id, prefix or name from the message
    ```

    Recovery releases the lease, and the runtime capacity slots of the session
    and its flow steps, only when it can prove the owner is gone: the owner's
    pid has exited (or now belongs to another process), the commands that
    Garuda recorded for it are stopped, and no process the owner started
    (they carry its lineage in `GARUDA_OWNER_LINEAGE`) is still running. If a
    leftover process is still running, the refusal names its pid. Stop it and
    run the command again. Recovery never takes a lease from a running owner.
    A lease that a session **quarantined** (`lease.quarantined`) is kept on
    purpose until its cleanup is reconciled. Recovery can't release it, so use
    `--isolation worktree` (or `--no-edits` for read-only work) meanwhile.
    The proof reads `/proc`, so on macOS recovery still refuses. Don't delete
    lease records by hand.

    If a dashboard or flow step's role worker is killed, the run no longer
    quarantines. Its supervisor proves that the worker's whole process tree,
    including detached children, has stopped, so the step fails with
    `flow.step_failed` ("the role worker exited without a result") and the
    lease and slot are released straight away, while the dashboard is still
    running. A lease that still reads `lease.quarantined` after a worker kill
    either comes from an older Garuda or was quarantined because the worker
    itself asked to keep it or its cleanup could not be proved (for example
    its supervisor was killed too). Recovery keeps refusing those.

    A flow run with `--check` whose step role is refused before its prompt
    (a logged-out harness gives `role.start_failed`; a model the harness no
    longer offers gives `role.model_unavailable`) stops with that code, as it
    does without `--check`. It releases its lease and slot, and
    `garuda flow resume FLOW_ID` retries the step once the harness is fixed.
    Older Garuda versions ended such a flow with `flow.cleanup_unproved` and
    quarantined it; start a new flow for those.

    A flow stopped by **SIGTERM** (`timeout`, `systemctl stop`, a CI cancel)
    now settles like Ctrl-C: the running step is cancelled and its worker tree
    reaped. For a `--check` flow whose step closed provably, the attempt is
    recorded as a settled `flow.interrupted` stop, the lease and slot are
    released, the command exits 143, and `garuda flow resume FLOW_ID` reruns
    the step under the original checks. If the flow was killed outright
    (`kill -9`) it stays `running` with a `lease.stale` and `capacity.stale`
    entry. `garuda runtime recover --session FLOW_ID` then releases them only
    when the flow's owner, every step worker, every process carrying its
    lineage, and every Docker container a confined step recorded are proved
    gone. Otherwise the refusal names the survivor, or gives the
    `docker rm -f NAME` to run. Recovery records the flow `interrupted`
    (`flow.owner_lost`). A checked flow killed mid-step can't be resumed,
    because that attempt never settled and its check evidence can't be proved.
    `flow resume` says so with `flow.interrupted_unsettled`, so start a new
    flow. Running `runtime recover` on a step session points you at its flow.

    A SIGTERM during the **check phase** (every step done) records the flow
    `interrupted`. `garuda flow resume FLOW_ID` runs the checks again on the
    unchanged candidate. A flow left `quarantined` there, with
    `lease.quarantined` in `doctor` and `flow resume` refusing
    `flow.recovery_required`, needs `garuda runtime recover --session FLOW_ID`
    first. That command releases the lease only when the owner is proved dead
    and every check has a terminal receipt or never launched. A check that may
    still be running keeps the quarantine. If the workspace changed since the
    check phase, the rerun refuses, so start a new flow.

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

??? question "`runtime handoff` refuses with `runtime.handoff_external_source`"

    Only a session the native runtime owns can be handed off. This one is
    owned by an external harness (a Claude or Codex role run, or an earlier
    handoff). Continue it on another runtime from its brief with
    `garuda run --resume S --as RUNTIME -t "..."`. To take it back natively
    once the harness has stopped, use `garuda runtime reclaim --session S`.

??? question "The Approvals page says `stale_request`, `already_answered` or `expired`"

    The page answered a request that is no longer the one it showed
    (`stale_request`: reload), one that already has an answer or a decision from
    the terminal, another tab or an earlier click (`already_answered`), or one
    past its expiry (`expired`; it is denied). "Answer recorded" only means the
    broker has the file: if the permission ceiling changed meanwhile, the broker
    denies it and says why in the session's approval decision.

??? question "A run refuses with `storage.unavailable: session store unavailable`"

    Garuda couldn't create or lock its session store (by default
    `~/.agent/sessions`, or `GARUDA_SESSIONS_DIR`), usually because the
    directory isn't writable by you. Nothing was started or written. Make the
    directory writable and owner-only (`chmod 700`), or point
    `GARUDA_SESSIONS_DIR` at one that is. `storage.corrupt_record` means a stored
    record is unreadable, malformed or from a newer Garuda version.

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

??? question "A harness fails to start: `initialize exceeded its 30s deadline`"

    The external harness did not answer its startup calls in time, which
    can happen on a loaded machine. Raise its deadline in your user
    `garuda.yaml`, for example
    `harnesses: {codex: {handshake_timeout_sec: 90}}` (5 to 600 seconds;
    default 30). See [Roles and flows](../guides/configuration.md#roles-and-flows-garudayaml).

??? question "A `consult` call is refused (`consult.*`)"

    The code says why, and a refused consult never runs the other role or
    spends its budget (except `consult.timeout`, `consult.failed`,
    `consult.unexpected_changes` and `consult.comparison_timeout`, which did run
    and say so in the receipt).

    - `consult.comparison_timeout`: the consulted role answered, but the
      read-only snapshot check that runs afterwards did not finish within its
      grace period, so the answer is withheld. This usually means a very large
      or slow workspace; retry, or consult on a smaller checkout.

    - `consult.not_granted`: the asking role's `consult:` list (user
      `garuda.yaml`) does not include the target. Projects cannot grant it.
    - `consult.nested`: a consulted role tried to consult.
    - `consult.limit`, `consult.too_long`: `consults.max_per_session` or the
      question size limit was reached; raise it in your user file.
    - `consult.busy`: another consult from this session (or the same request id)
      is still in progress; consults run one at a time per asker.
    - `consult.capacity_unavailable`: the target's harness is at its
      `max_parallel`; consults never wait for a slot.
    - `consult.spend_unsupported`: a run-tree token or dollar cap is active,
      but the target uses ACP, whose model calls Garuda cannot meter. Choose a
      native consult target to keep the run-tree cap enforced.
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

    - `agent.not_found`: no agent with that name, or no file at that path.
      `garuda agent list` shows the names; `garuda/build` is always the
      packaged agent.
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

??? question "`garuda recipe run` refuses with `recipe.*`"

    The recipe is checked before anything runs. The message names the field.

    - `recipe.invalid`: an unknown key, an empty `steps` list, a step without
      `agent` or `prompt`, an unknown `mode`, a `{{name}}` that isn't a declared
      parameter, or a file that isn't valid YAML.
    - `recipe.not_found` or `recipe.unreadable`: check the recipe path.
    - `recipe.agent_not_found`: `garuda agent list` shows the agent names.
    - `recipe.param_missing`: pass `--param NAME=VALUE` or give it a `default`.
    - `recipe.param_unknown`: the `--param` isn't declared under `parameters`.

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
    `pi`, `goose`, `omp`). `doctor` only checks the harnesses it recognises, so a
    misspelled one is skipped rather than reported.

??? question "A flow stops with `flow.output_missing`"

    The step didn't end with the `<garuda-artifact type="…">` block the next
    step needs. For a native role, the block must be in the summary it passes to
    `task_complete`. Give the role an agent that says so; see
    [Level 5](../use-cases/teams.md#run-a-plan-build-review-flow).

??? question "A flow stops with `flow.artifact_malformed`"

    The step's answer has a `</garuda-artifact>` tag outside any block, or a
    `<garuda-artifact>` block (or one quoted inside it) that never closes.
    Garuda stops instead of guessing where the artifact ends. A complete
    envelope quoted inside a block is fine; the stop message names the line.
    Ask the role's agent to close each block once, at the end of its answer.
    A checked flow (`--check`) stopped this way, or with
    `flow.artifact_too_large`, resumes with `garuda flow resume FLOW_ID`,
    which runs that step again.

??? question "A read-only flow step runs until it hits its turn limit"

    The native completion checker can ask a read-only step for evidence it
    isn't allowed to collect. Give read-only roles an agent with
    `completion: {verifier: false}`, as shown in
    [Level 5](../use-cases/teams.md#run-a-plan-build-review-flow).

??? question "A flow stops with `flow.review_not_independent`"

    The reviewer would run as the same harness and model as the role it
    reviews, one of that role's fallbacks, or a role it may consult. Give the
    reviewer a different model, or remove the reviewer from the reviewed role's
    `consult:` list. When the configured roles already collide, the flow stops
    before its first step (nothing ran); otherwise it stops when the reviewer is
    selected, after a fallback or consult made them share an identity.

??? question "A flow stops with `flow.review_candidate_unknown`"

    The reviewed step's workspace version can't tell candidates apart, usually
    because the workspace isn't a git work tree, so Garuda can't prove the
    reviewer saw what the step produced. Run the flow in a git repository, or
    set `independent: false` on the review to accept an unenforced binding.

??? question "`garuda flow show` says `flow.session_not_found` or `flow.not_a_flow`"

    `flow show` and `flow resume` take the flow ID that `garuda flow run`
    prints at the end (`"flow_session": "…"`, for a stopped flow too), a unique
    prefix of it, or `latest`. Prefixes and `latest` are looked up in the
    current project; pass `--workspace` from elsewhere. `flow.not_a_flow`
    means the ID names a session that isn't a flow, such as one flow step.

??? question "`garuda flow resume` stops with `flow.review_unresolved`"

    The reviewed step finished, but its reviewer never approved it: the review
    was invalid, still asked for changes after the last round, or never ran. A
    checked CLI flow can retry a settled reviewer failure before checks start
    when its saved review checkpoint and candidate still match. If resume
    reports `flow.review_unresolved`, start a new flow.

??? question "A checked flow refuses resume with `flow.verification_reconciliation_required`"

    Garuda could not prove the original check request, stopped workspace and
    evidence, or a consistent check phase. Checked flows created before the
    resume checkpoint was introduced have no saved request. Start a new flow
    after inspecting the workspace and any retained check records. If you need
    to change configuration to fix a confinement refusal, use a new checked
    flow with the updated configuration.

??? question "A flow stops with `flow.review_invalid`"

    The review block must be plain lines: `verdict: approve` or
    `verdict: changes`, then `findings:` and one `- [blocker|major|minor|nit] …`
    line per finding. Garuda gives the reviewer this format; a reviewer that
    adds Markdown or prose inside the block is refused rather than guessed at.
    An approval with nothing to report leaves `findings:` empty; a common
    "no findings" line such as `- none.` is accepted under `verdict: approve`
    only. A checked flow (`--check`) stopped this way resumes with
    `garuda flow resume FLOW_ID`, which runs the reviewer again on the same
    candidate.

??? question "`garuda flow resume` stops with `flow.stop_not_resumable`"

    The checked flow stopped before its checks with a code that a resume does
    not continue, for example `flow.review_changes_requested`. The message
    names that code. Nothing is wrong with the check records; start a new flow.

??? question "A read-only external role refuses with `workspace.readonly_unenforced`"

    A role on an external harness with `permissions: readonly` runs only in a
    Docker container that Garuda proves is confined. Set
    `harnesses.<id>.confinement.image` in your `garuda.yaml` to an image where
    you installed and logged in to the harness, and make sure Docker is
    running. Garuda never runs such a role on the host instead.
    For a flow, a missing image on a statically resolved read-only ACP step stops
    the flow before any step runs. The CLI prints the flow session ID and
    `workspace.readonly_unenforced`.

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

For a smaller attachment, preview selected categories first and inspect the
exact JSON before exporting it:

```bash
garuda runtime support --session latest --select causes,identities,review,acceptance --preview
garuda runtime support --session latest --select causes,identities,review,acceptance --export > diagnostic.json
```

`unavailable` means the selected session never retained that fact or its bounded
record could not be read. It is not evidence that the event did not happen.
`clipped` and `withheld` are similarly explicit; support output never fills
those gaps with a transcript, current configuration, or a provider probe.
