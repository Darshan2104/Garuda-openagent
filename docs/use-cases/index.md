# Use cases

Short recipes ordered from **easy to hard**. Each one says when to use it, gives
a command you can copy, explains what happens, and lists what to watch for.

New to Garuda? Do the [Quickstart](../guides/getting-started.md) first, then
start at Level 1.

<div class="grid cards" markdown>

-   **Level 1 · Explore safely**

    Read and understand code without changing it.

    [:octicons-arrow-right-24: Open](explore.md)

-   **Level 2 · Change code**

    Fix bugs, approve risky actions, verify with your tests.

    [:octicons-arrow-right-24: Open](change-code.md)

-   **Level 3 · Teach it your project**

    Instructions, your own agents, skills, MCP, hooks.

    [:octicons-arrow-right-24: Open](customize.md)

-   **Level 4 · Run work in parallel**

    Background runs, worktrees, checked merges, usage.

    [:octicons-arrow-right-24: Open](parallel.md)

-   **Level 5 · Build a team of roles**

    Roles, plan → build → review flows, consults, project checks.

    [:octicons-arrow-right-24: Open](teams.md)

-   **Level 6 · Automate**

    Scripts, CI, the Python SDK, and an HTTP service.

    [:octicons-arrow-right-24: Open](automate.md)

-   **Level 7 · Advanced**

    Claude Code, Codex and other harnesses, handoffs, routing.

    [:octicons-arrow-right-24: Open](advanced.md)

</div>

## Level 1 · Explore safely

Read-only recipes. [Open Level 1 →](explore.md)

| I want to… | Run |
|---|---|
| [Understand a codebase](explore.md#ask-questions-about-a-codebase) | `garuda run --mode readonly -t "…"` |
| [Be sure nothing changed](explore.md#make-sure-a-run-changes-nothing) | `garuda run --no-edits -t "…"` |
| [Get a review of my changes](explore.md#review-your-uncommitted-changes) | `garuda run --agent reviewer --mode readonly -t "…"` |
| [Plan a change first](explore.md#plan-a-change-before-making-it) | `garuda run --agent plan -t "…"` |
| [Ask a follow-up](explore.md#ask-a-follow-up-question) | `garuda run --mode readonly --resume latest -t "…"` |
| [Read PDFs and spreadsheets](explore.md#read-pdfs-and-spreadsheets) | `garuda run --mode readonly -t "Summarize report.pdf"` |
| [Browse past runs](explore.md#browse-past-runs-in-the-dashboard) | `garuda web --read-only` |

## Level 2 · Change code

Garuda edits files and runs commands. [Open Level 2 →](change-code.md)

| I want to… | Run |
|---|---|
| [Fix a bug, with Git as a safety net](change-code.md#fix-a-bug-with-a-git-safety-net) | `garuda run -t "…"` |
| [Approve risky actions myself](change-code.md#chat-and-approve-risky-actions-yourself) | `garuda chat` |
| [Work in a browser](change-code.md#work-with-the-agent-in-your-browser) | `garuda web --allow-workspace .` |
| [Count it done only if my tests pass](change-code.md#verify-the-result-with-your-own-check) | `garuda run --check "pytest -q" -t "…"` |
| [Have the agent gather evidence](change-code.md#make-garuda-prove-the-fix) | `garuda run --mode rigorous -t "…"` |
| [Run untrusted code](change-code.md#run-untrusted-code-in-docker) | `garuda run --workspace-kind docker --no-network -t "…"` |
| [Limit writes and network](change-code.md#limit-writes-and-network-with-the-os-sandbox) | `garuda run --workspace-kind sandbox -t "…"` |
| [Cap time and turns](change-code.md#cap-time-turns-and-cost) | `garuda run --deadline-sec 900 --max-turns 30 -t "…"` |
| [Reuse what an earlier run did](change-code.md#name-a-session-and-build-on-it) | `garuda run -t "… @fix-tests …"` |
| [Try another model](change-code.md#use-a-different-model-for-one-run) | `garuda run --model provider/model -t "…"` |

## Level 3 · Teach it your project

Project files that shape every run. [Open Level 3 →](customize.md)

| I want to… | Add |
|---|---|
| [Give standing instructions](customize.md#give-garuda-project-instructions) | `AGENTS.md` at the project root |
| [Make my own agent](customize.md#create-your-own-agent) | `garuda agent new <name> --from garuda/build` |
| [See what an agent will do](customize.md#see-exactly-what-an-agent-will-do) | `garuda agent show <name>` |
| [Get JSON back](customize.md#get-structured-json-back) | `output.schema` in the agent |
| [Let it propose notes to remember](customize.md#let-the-agent-propose-notes-to-remember) | `garuda memory review` |
| [Teach a repeatable procedure](customize.md#add-a-skill) | `.agent/skills/<name>/SKILL.md` |
| [Use tools from an MCP server](customize.md#connect-mcp-tools) | `.agent/mcp.json`, then `garuda mcp trust` |
| [Block or log tool calls](customize.md#block-or-log-tool-calls-with-a-hook) | `hooks:` in `~/.agent/settings.yaml` |
| [Write my own tool in Python](customize.md#add-your-own-python-tools) | `.agent/tools/<name>.py` |

## Level 4 · Run work in parallel

Background runs, separate worktrees, and checked merges.
[Open Level 4 →](parallel.md)

| I want to… | Run |
|---|---|
| [Get my terminal back](parallel.md#run-a-task-in-the-background) | `garuda run --bg -t "…"` |
| [Let two tasks edit at once](parallel.md#let-two-tasks-edit-at-once) | `garuda run --isolation worktree -t "…"` |
| [Merge only if checks pass](parallel.md#check-and-merge-a-worktree-session) | `garuda sessions merge <name> --check "…"` |
| [Answer an approval from elsewhere](parallel.md#answer-an-approval-from-another-terminal-or-the-browser) | `garuda approvals answer <session> <id> --allow` |
| [Cap runs per runtime](parallel.md#limit-how-many-runs-happen-at-once) | `capacity:` in `~/.agent/settings.yaml` |
| [Watch everything](parallel.md#watch-it-all-in-the-dashboard) | `garuda web` |
| [See usage, cost and limits](parallel.md#see-usage-cost-and-limits) | `garuda web --read-only` → Usage |

## Level 5 · Build a team of roles

Roles, flows, reviews, consults, and project checks.
[Open Level 5 →](teams.md)

| I want to… | Run |
|---|---|
| [Set up roles](teams.md#set-up-your-roles) | `garuda init`, `garuda doctor` |
| [Run as a role](teams.md#run-a-task-as-a-role) | `garuda run --role coder -t "…"` |
| [Plan, build and review in one go](teams.md#run-a-plan-build-review-flow) | `garuda flow run plan-build-review -t "…"` |
| [Write my own flow](teams.md#write-your-own-flow) | `garuda config show --flow <name>` |
| [Let one role ask another](teams.md#let-one-role-ask-another) | `consult: [planner]` on a role |
| [Require checks in this project](teams.md#require-checks-for-every-run-in-this-project) | `garuda config trust` |
| [Keep a role from changing anything](teams.md#keep-a-role-from-changing-anything) | `write_policy: no-edits` |

## Level 6 · Automate

Run Garuda from files, scripts, and programs. [Open Level 6 →](automate.md)

| I want to… | Use |
|---|---|
| [Chain steps (plan → build → test)](automate.md#run-a-multi-step-recipe) | `garuda recipe run fix-and-test.yaml` |
| [Run in a script or CI job](automate.md#use-garuda-in-scripts-and-ci) | `garuda run --json -t "…"` |
| [Start many and poll them](automate.md#use-garuda-in-scripts-and-ci) | `garuda run --bg`, `garuda sessions show --json` |
| [Embed in a Python program](automate.md#call-garuda-from-python) | `SoftwareAgent` |
| [Submit jobs over HTTP](automate.md#call-garuda-over-http) | `garuda serve` |

## Level 7 · Advanced

External harnesses, handoffs, routing, and a second model.
[Open Level 7 →](advanced.md)

| I want to… | Run |
|---|---|
| [See which harnesses are ready](advanced.md#see-which-external-harnesses-are-ready) | `garuda runtime list` |
| [Use Claude Code, Codex, …](advanced.md#run-a-task-with-claude-code-codex-or-another-harness) | `garuda run --runtime claude -t "…"` |
| [Hand a session to a harness](advanced.md#hand-off-a-garuda-session-to-an-external-harness) | `garuda runtime handoff --session latest --to codex` |
| [Recover after an interruption](advanced.md#recover-or-take-back-a-session) | `garuda runtime recover --session latest` |
| [Continue on another runtime](advanced.md#recover-or-take-back-a-session) | `garuda run --resume latest --as claude -t "…"` |
| [Skip a harness that hit its limit](advanced.md#skip-a-harness-whose-subscription-limit-is-used-up) | a role `fallback` |
| [Choose the runtime automatically](advanced.md#route-tasks-to-runtimes-automatically) | `routing:` in `~/.agent/settings.yaml` |
| [Add a cheaper investigation model](advanced.md#add-a-cheaper-model-for-investigation) | `collection:` in `~/.agent/settings.yaml` |
| [Benchmark and evaluate](advanced.md#benchmark-and-evaluate) | `garuda run --mode eval --trajectory run.jsonl -t "…"` |
