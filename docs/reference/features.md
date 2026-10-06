# Feature index

Every Garuda feature on one page: what it gives you, a command to try, and
where it is demonstrated step by step. CI checks that every public command
appears in a use-case page or the [cheat sheet](cheat-sheet.md).

## Run and change code

| Feature | What you get | Try it | Demo |
|---|---|---|---|
| Headless run | One task, an answer, a saved session | `garuda run -t "…"` | [Level 2](../use-cases/change-code.md#fix-a-bug-with-a-git-safety-net) |
| Interactive chat | Turn-by-turn work with `y/N` approvals | `garuda chat` | [Level 2](../use-cases/change-code.md#chat-and-approve-risky-actions-yourself) |
| Read-only mode | Inspection only, blocked writes | `garuda run --mode readonly -t "…"` | [Level 1](../use-cases/explore.md#ask-questions-about-a-codebase) |
| No-edits guard | A run that is checked to have changed nothing | `garuda run --no-edits -t "…"` | [Level 1](../use-cases/explore.md#make-sure-a-run-changes-nothing) |
| Acceptance checks | "Done" means your command passed | `garuda run --check "pytest -q" -t "…"` | [Level 2](../use-cases/change-code.md#verify-the-result-with-your-own-check) |
| Evidence modes | The agent proves its own work before stopping | `garuda run --mode rigorous -t "…"` | [Level 2](../use-cases/change-code.md#make-garuda-prove-the-fix) |
| Budgets | Wall-clock and turn limits | `garuda run --deadline-sec 900 --max-turns 30 -t "…"` | [Level 2](../use-cases/change-code.md#cap-time-turns-and-cost) |
| Any model | A LiteLLM `provider/model`, reasoning effort | `garuda run --model provider/model -t "…"` | [Level 2](../use-cases/change-code.md#use-a-different-model-for-one-run) |
| Documents | PDF and spreadsheet readers | `garuda run --mode readonly -t "Summarize report.pdf"` | [Level 1](../use-cases/explore.md#read-pdfs-and-spreadsheets) |

## Where commands run

| Feature | What you get | Try it | Demo |
|---|---|---|---|
| Docker workspace | Commands in a container, optionally offline | `garuda run --workspace-kind docker --no-network -t "…"` | [Level 2](../use-cases/change-code.md#run-untrusted-code-in-docker) |
| OS sandbox | Bubblewrap or Seatbelt limits on writes and network | `garuda run --workspace-kind sandbox -t "…"` | [Level 2](../use-cases/change-code.md#limit-writes-and-network-with-the-os-sandbox) |
| Remote Docker | A container on another Docker host | `garuda run --workspace-kind remote --docker-host ssh://host -t "…"` | [Safety guide](../guides/safety-and-workspaces.md#remote-docker-workspace) |
| Worktree sessions | Each session edits its own Git branch | `garuda run --isolation worktree -t "…"` | [Level 4](../use-cases/parallel.md#let-two-tasks-edit-at-once) |
| Checked merge | Merge a worktree only if checks pass in Docker | `garuda sessions merge NAME --check "…"` | [Level 4](../use-cases/parallel.md#check-and-merge-a-worktree-session) |

## Sessions

| Feature | What you get | Try it | Demo |
|---|---|---|---|
| Session record | Every run saved with its events and state | `garuda sessions show latest` | [Level 1](../use-cases/explore.md#ask-a-follow-up-question) |
| Resume | Continue a conversation | `garuda run --resume latest -t "…"` | [Level 1](../use-cases/explore.md#ask-a-follow-up-question) |
| Names and briefs | Hand one run's outcome to another | `garuda run -t "… @fix-tests …"` | [Level 2](../use-cases/change-code.md#name-a-session-and-build-on-it) |
| Background runs | Queue a task and get your terminal back | `garuda run --bg -t "…"` | [Level 4](../use-cases/parallel.md#run-a-task-in-the-background) |
| Cancel | Remove from the queue or stop the worker | `garuda sessions cancel NAME` | [Level 4](../use-cases/parallel.md#run-a-task-in-the-background) |
| Capacity | At most N runs per runtime; the rest queue | `capacity:` in `~/.agent/settings.yaml` | [Level 4](../use-cases/parallel.md#limit-how-many-runs-happen-at-once) |
| Approvals from elsewhere | Answer a waiting prompt from another terminal | `garuda approvals answer latest ID --allow` | [Level 4](../use-cases/parallel.md#answer-an-approval-from-another-terminal-or-the-browser) |
| Trajectories | Export a run's events | `garuda run --trajectory run.jsonl -t "…"` | [Level 6](../use-cases/automate.md#use-garuda-in-scripts-and-ci) |

## Agents and project knowledge

| Feature | What you get | Try it | Demo |
|---|---|---|---|
| Project instructions | `AGENTS.md` in every run | `AGENTS.md` at the project root | [Level 3](../use-cases/customize.md#give-garuda-project-instructions) |
| Agent definitions | Your own agents that extend packaged ones | `garuda agent new NAME --from garuda/build` | [Level 3](../use-cases/customize.md#create-your-own-agent) |
| Agent inspection | Every effective setting and the exact prompt | `garuda agent show NAME`, `garuda agent prompt NAME` | [Level 3](../use-cases/customize.md#see-exactly-what-an-agent-will-do) |
| Agent check | Catch mistakes before a run | `garuda agent check NAME` | [Level 3](../use-cases/customize.md#create-your-own-agent) |
| Profile migration | Move an old profile to version 1 | `garuda agent migrate PATH` | [Level 3](../use-cases/customize.md#create-your-own-agent) |
| Structured output | A JSON result checked against a schema | `output.schema` in an agent | [Level 3](../use-cases/customize.md#get-structured-json-back) |
| Memory notes | The agent proposes; you accept | `garuda memory review` | [Level 3](../use-cases/customize.md#let-the-agent-propose-notes-to-remember) |
| Skills | Procedures loaded on demand, filtered per agent | `.agent/skills/NAME/SKILL.md` | [Level 3](../use-cases/customize.md#add-a-skill) |
| Tool options | Bash timeouts, web domain allowlists, subagent limits | `tools.options` in an agent | [Agents guide](../guides/agents.md#tools) |
| MCP tools | Tools from MCP servers, trusted per entry | `garuda mcp list`, `garuda mcp trust` | [Level 3](../use-cases/customize.md#connect-mcp-tools) |
| Hooks | Your script blocks or logs tool calls | `hooks:` in `~/.agent/settings.yaml` | [Level 3](../use-cases/customize.md#block-or-log-tool-calls-with-a-hook) |
| Python tools | Your own tools, opt-in | `garuda run --load-project-tools -t "…"` | [Level 3](../use-cases/customize.md#add-your-own-python-tools) |

## Roles, flows and teams

| Feature | What you get | Try it | Demo |
|---|---|---|---|
| Setup and diagnostics | Proposed roles; checks with fixes | `garuda init`, `garuda doctor` | [Level 5](../use-cases/teams.md#set-up-your-roles) |
| Effective configuration | Every value and where it came from | `garuda config show` | [Level 5](../use-cases/teams.md#set-up-your-roles) |
| Roles | A harness and exact model per job | `garuda run --role coder -t "…"` | [Level 5](../use-cases/teams.md#run-a-task-as-a-role) |
| Fallbacks | Start on another harness if one is missing, logged out or out of quota | `fallback:` on a role | [Level 5](../use-cases/teams.md#run-a-task-as-a-role) |
| Flows | Plan → build → review in one command | `garuda flow run plan-build-review -t "…"` | [Level 5](../use-cases/teams.md#run-a-plan-build-review-flow) |
| Custom flows | Your own steps and reviews | `garuda config show --flow NAME` | [Level 5](../use-cases/teams.md#write-your-own-flow) |
| Flow recovery | Continue after the last finished step | `garuda flow resume FLOW_ID` | [Level 5](../use-cases/teams.md#run-a-plan-build-review-flow) |
| Consults | One role asks another, read-only | `consult: [planner]` on a role | [Level 5](../use-cases/teams.md#let-one-role-ask-another) |
| Project checks | Every run in a repo verified by its tests | `garuda config trust` | [Level 5](../use-cases/teams.md#require-checks-for-every-run-in-this-project) |
| Read-only external roles | A harness in a proven read-only container | `confinement.image` in `garuda.yaml` | [Level 5](../use-cases/teams.md#keep-a-role-from-changing-anything) |
| Config migration | `settings.yaml` limits into `garuda.yaml` | `garuda config migrate` | [Level 5](../use-cases/teams.md#move-runtime-limits-into-garudayaml) |

## Watch and measure

| Feature | What you get | Try it | Demo |
|---|---|---|---|
| Dashboard | Runs, sessions, approvals, setup, chat | `garuda web` | [Level 4](../use-cases/parallel.md#watch-it-all-in-the-dashboard) |
| Usage ledger | Calls, tokens and known cost by model and role | `garuda web --read-only` → Usage | [Level 4](../use-cases/parallel.md#see-usage-cost-and-limits) |
| Provider limits | Login and quota readings with their source | `garuda web --read-only` → Providers | [Level 4](../use-cases/parallel.md#see-usage-cost-and-limits) |
| JSON everywhere | Machine-readable sessions, doctor, agents | `garuda sessions show latest --json` | [Level 6](../use-cases/automate.md#use-garuda-in-scripts-and-ci) |

## Automate

| Feature | What you get | Try it | Demo |
|---|---|---|---|
| Recipes | Parameterised multi-step YAML | `garuda recipe run file.yaml` | [Level 6](../use-cases/automate.md#run-a-multi-step-recipe) |
| CI and scripts | Exit status and JSON events | `garuda run --json -t "…"` | [Level 6](../use-cases/automate.md#use-garuda-in-scripts-and-ci) |
| Python SDK | `SoftwareAgent`, conversations, your own tools | `await SoftwareAgent(...).run("…")` | [Level 6](../use-cases/automate.md#call-garuda-from-python) |
| `AgentSpec` | Load and narrow an agent from Python | `AgentSpec.load(NAME).narrow(...)` | [Level 6](../use-cases/automate.md#call-garuda-from-python) |
| HTTP service | JSON-RPC jobs with an agent allowlist | `garuda serve --allow-agent reviewer` | [Level 6](../use-cases/automate.md#call-garuda-over-http) |

## External harnesses

| Feature | What you get | Try it | Demo |
|---|---|---|---|
| Runtime catalog | Which harnesses are installed and ready | `garuda runtime list` | [Level 7](../use-cases/advanced.md#see-which-external-harnesses-are-ready) |
| ACP runs | Claude Code, Codex and others on your login | `garuda run --runtime claude -t "…"` | [Level 7](../use-cases/advanced.md#run-a-task-with-claude-code-codex-or-another-harness) |
| Handoff | Move a native session to a harness | `garuda runtime handoff --session latest --to codex` | [Level 7](../use-cases/advanced.md#hand-off-a-garuda-session-to-an-external-harness) |
| Recover and reclaim | Repair ownership after an interruption | `garuda runtime recover --session latest` | [Level 7](../use-cases/advanced.md#recover-or-take-back-a-session) |
| Continue elsewhere | Resume on another runtime from a brief | `garuda run --resume latest --as claude -t "…"` | [Level 7](../use-cases/advanced.md#recover-or-take-back-a-session) |
| Routing | Rules choose the starting runtime | `routing:` in `~/.agent/settings.yaml` | [Level 7](../use-cases/advanced.md#route-tasks-to-runtimes-automatically) |
| Quota fallback | Skip a harness whose plan limit is reached | a role `fallback` | [Level 7](../use-cases/advanced.md#skip-a-harness-whose-subscription-limit-is-used-up) |
| Collection model | A cheaper model for read-only investigation | `collection:` in settings | [Level 7](../use-cases/advanced.md#add-a-cheaper-model-for-investigation) |
| ACP server | Use Garuda from an ACP editor | `python -m garuda.acp.server` | [Level 7](../use-cases/advanced.md#use-garuda-from-an-acp-editor) |
| Evaluation | Benchmark modes and paired reports | `garuda eval dual-model report …` | [Evaluation](../evaluation/index.md) |
