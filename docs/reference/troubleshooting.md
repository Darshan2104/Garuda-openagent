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
      allowed. See [custom profiles](../use-cases/customize.md#create-your-own-agent-profile).

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

    Another live session holds a lease that allows changes to that workspace.
    Garuda refuses overlapping sessions so their changes can't be mixed up.
    Wait for the other run to finish. If that run crashed, its lease stops being
    renewed and a later run can take it over once it goes stale.

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

    Its worker is waiting for a slot: the harness is at its `max_parallel`
    (`capacity` in `settings.yaml`), shared with foreground runs. See what holds
    the slot with `garuda sessions`; a session that reads `crashed` lost its
    worker, and its slot is reclaimed automatically once the process is
    confirmed dead. `garuda sessions cancel SESSION` removes a queued session
    or stops a running one. The worker's output is in
    `<sessions dir>/<id>/worker.log`, cut at 1 MB.

??? question "`sessions cancel` says the worker's pid was reused"

    The recorded process identity no longer matches that pid, so nothing was
    signalled; the session is marked failed. Garuda never signals a process it
    cannot prove is its worker.

??? question "A runtime shows as unavailable"

    Run `garuda runtime inspect <id>`. Common causes: the vendor CLI isn't
    installed or isn't on `PATH`, the runtime is disabled in
    `~/.agent/settings.yaml`, or its version check fails. Login and quota often
    show as `unknown`; that alone does not make a runtime unavailable.

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
    sessions, then run `garuda doctor --recover-project-ids`. Sessions whose
    repository is still where it was (same path and filesystem identity) move
    to the new key with their names; any others keep their old id and still
    resolve by full ID.

## Still stuck?

Generate a redacted support bundle and attach it to an
[issue](https://github.com/Darshan2104/Garuda-openagent/issues):

```bash
garuda runtime support --session latest
```
