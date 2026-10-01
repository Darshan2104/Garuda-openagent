# Command builder

Pick what you want to do and where it should run. The command updates as you
click, and the copy button copies it as shown.

<div class="gb" data-garuda-builder data-command="garuda run" markdown>

<div class="gb-step">
<p class="gb-label">1 · What do you want to do?</p>
<div class="gb-options" data-group="goal">
<button data-args="--mode readonly" data-task="Explain how this project is organized and where the entry points are" data-explain="--mode readonly blocks Garuda's file-writing tools and any shell command that isn't a plain inspection command. It is a guardrail with known gaps, not a sandbox.">Understand code</button>
<button data-args="--agent reviewer --mode readonly" data-task="Review the changes in review.diff and list concrete problems" data-explain="--agent reviewer uses the built-in read-only reviewer profile." data-note="Read-only runs can't call git. Save the diff first: git diff HEAD &gt; review.diff">Review changes</button>
<button data-args="--agent plan" data-task="Plan how to add input validation to the signup form" data-explain="--agent plan can read and inspect, but its profile denies file edits.">Plan a change</button>
<button data-args="" data-task="Find and fix the failing test" data-explain="The default build profile can edit files and run commands. Dangerous commands are refused, and actions that need approval are denied in a headless run (use garuda chat to approve them).">Fix a bug</button>
<button data-args="--mode rigorous" data-task="Fix the failing tests and show evidence that they pass" data-explain="--mode rigorous adds planning, critique and repair, plus acceptance and evidence checks before Garuda accepts the result. It makes extra model calls." data-note="Rigorous mode costs noticeably more model calls than the default.">Fix and prove it</button>
</div>
</div>

<div class="gb-step">
<p class="gb-label">2 · Where should commands run?</p>
<div class="gb-options" data-group="where">
<button data-args="" data-explain="Commands run on your machine as you, in the current directory.">My machine</button>
<button data-args="--workspace-kind sandbox" data-native-only data-explain="--workspace-kind sandbox uses the OS sandbox (Bubblewrap or macOS Seatbelt) to limit writes and network. It does not stop the agent reading host files." data-note="The sandbox refuses to start if no OS backend is available.">OS sandbox</button>
<button data-args="--workspace-kind docker --no-network" data-native-only data-explain="--workspace-kind docker runs commands in a container with the project mounted at /workspace; --no-network turns off container networking." data-note="Needs a running Docker daemon. The default image is ubuntu:22.04; add --docker-image python:3.12 (or similar) when you need a toolchain.">Docker, no network</button>
<button data-args="--workspace-kind docker" data-native-only data-explain="--workspace-kind docker runs commands in a container with the project mounted at /workspace and bridged networking." data-note="Needs a running Docker daemon.">Docker with network</button>
</div>
</div>

<div class="gb-step">
<p class="gb-label">3 · Which agent does the work?</p>
<div class="gb-options" data-group="engine">
<button data-args="" data-explain="Garuda's own agent loop does the work, with Garuda's tools, permission rules and completion checks.">Garuda (native)</button>
<button data-args="--runtime claude" data-external data-explain="--runtime claude starts Claude Code through ACP, using your own Claude Code login." data-note="External harnesses run as their own process on your machine with their own permissions. Garuda records the session and workspace changes, but it does not verify the result, and its run modes, permission rules and workspace kinds do not apply. Install and log in to the vendor CLI first.">Claude Code</button>
<button data-args="--runtime codex" data-external data-explain="--runtime codex starts Codex through ACP, using your own Codex login." data-note="External harnesses run as their own process on your machine with their own permissions. Garuda records the session and workspace changes, but it does not verify the result, and its run modes, permission rules and workspace kinds do not apply. Install and log in to the vendor CLI first.">Codex</button>
<button data-args="--runtime opencode" data-external data-explain="--runtime opencode starts OpenCode through ACP, using OpenCode's own configuration." data-note="External harnesses run as their own process on your machine with their own permissions. Garuda records the session and workspace changes, but it does not verify the result, and its run modes, permission rules and workspace kinds do not apply. Install and log in to the vendor CLI first.">OpenCode</button>
<button data-args="--runtime cursor" data-external data-explain="--runtime cursor starts Cursor Agent through ACP, using your own Cursor login." data-note="External harnesses run as their own process on your machine with their own permissions. Garuda records the session and workspace changes, but it does not verify the result, and its run modes, permission rules and workspace kinds do not apply. Install and log in to the vendor CLI first.">Cursor</button>
<button data-args="--runtime pi" data-external data-explain="--runtime pi starts Pi through ACP, using your own Pi login." data-note="External harnesses run as their own process on your machine with their own permissions. Garuda records the session and workspace changes, but it does not verify the result, and its run modes, permission rules and workspace kinds do not apply. Install and log in to the vendor CLI first.">Pi</button>
<button data-args="--runtime goose" data-external data-explain="--runtime goose starts Goose through ACP, using Goose's own configuration." data-note="External harnesses run as their own process on your machine with their own permissions. Garuda records the session and workspace changes, but it does not verify the result, and its run modes, permission rules and workspace kinds do not apply. Install and log in to the vendor CLI first.">Goose</button>
</div>
</div>

<div class="gb-step">
<p class="gb-label">4 · Extras (optional, pick any)</p>
<div class="gb-options" data-group="extras" data-multi>
<button data-args="--deadline-sec 900" data-native-only data-explain="--deadline-sec 900 gives the run a 15-minute wall-clock budget.">15-minute limit</button>
<button data-args="--max-turns 30" data-native-only data-explain="--max-turns 30 stops the loop after 30 agent turns.">30-turn limit</button>
<button data-args="--resume latest" data-native-only data-explain="--resume latest continues your most recent session as a new, linked session.">Continue last session</button>
<button data-args="--json" data-native-only data-explain="--json prints JSONL events instead of the human-readable result.">JSON output</button>
<button data-args="--trajectory run.jsonl" data-native-only data-explain="--trajectory run.jsonl also saves the run's events to run.jsonl.">Save trajectory</button>
</div>
</div>

<div class="gb-step">
<p class="gb-label">5 · Your task</p>
<input class="gb-task" type="text" aria-label="Task text" spellcheck="false">
</div>

<p class="gb-label">Your command</p>

<div class="gb-output" markdown>

```bash
garuda run --mode readonly -t "Explain how this project is organized and where the entry points are"
```

</div>

<ul class="gb-explain"></ul>
<p class="gb-note"></p>

</div>

!!! tip "Before you run it"
    - Run from the project directory, or add `--workspace /path/to/project`.
    - Native runs need a model credential. See [Quickstart](getting-started.md#2-add-a-model-key).
    - Check the selected model printed at startup. Model calls can cost money.
    - For untrusted code, choose **Docker, no network**. See
      [Safety and workspaces](safety-and-workspaces.md).

## What the builder does not cover

The builder only produces `garuda run` commands. For everything else:

| You want to… | Go to |
|---|---|
| Talk to the agent and approve risky actions yourself | [`garuda chat`](../use-cases/change-code.md#chat-and-approve-risky-actions-yourself) |
| Browse past runs in a browser | [Web dashboard](../use-cases/explore.md#browse-past-runs-in-the-dashboard) |
| Hand a session to Claude Code, Codex, … | [Hand off a session](../use-cases/advanced.md#hand-off-a-garuda-session-to-an-external-harness) |
| Script Garuda from Python or HTTP | [Level 4 · Automate](../use-cases/automate.md) |
| See every flag | [CLI reference](../reference/cli.md) or `garuda run --help` |
