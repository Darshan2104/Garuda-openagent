---
hide:
  - navigation
  - toc
---

<div class="gd-hero" markdown>

# Garuda

A safe, inspectable runtime for AI coding agents. Give it a task and it runs a
model in a workspace you choose, screens every action, and verifies the result
with checks you trust. Run tasks in the background and in parallel, give each
job its own model as a team of roles, or drive external agents such as Claude
Code or Codex. Every run is recorded so you can inspect or continue it.

[Get started in 5 minutes](guides/getting-started.md){ .md-button .md-button--primary }
[Browse use cases](use-cases/index.md){ .md-button }

</div>

## Try it

```bash
git clone https://github.com/Darshan2104/Garuda-openagent.git && cd Garuda-openagent
python3.12 -m venv .venv && source .venv/bin/activate && pip install -e .
export OPENROUTER_API_KEY=sk-or-...   # placeholder: use your own key
garuda run --mode readonly -t "Summarize this repository"
```

The last command runs in read-only mode, which blocks Garuda's file-writing
tools. Model calls can cost money.

## How a run works

Click a step to learn more.

```mermaid
flowchart LR
  task(["1 · Your task"]) --> runtime{"2 · Runtime"}
  runtime --> work["3 · Agent works<br/>in your workspace"]
  work --> checks{"4 · Completion<br/>checks"}
  checks -- "not yet" --> work
  checks -- accepted --> session[("5 · Session<br/>record")]
  click runtime "guides/how-garuda-works/#1-runtime-who-does-the-work"
  click work "guides/how-garuda-works/#3-workspace-where-commands-run"
  click checks "guides/how-garuda-works/#4-run-mode-when-work-counts-as-done"
  click session "guides/how-garuda-works/#5-session-what-gets-recorded"
```

With Garuda's native agent, every tool call in step 3 is screened by
permission rules. With an external harness such as Claude Code, the harness
does step 3 with its own authority and step 4 is skipped: Garuda records the
result but does not verify it.

## Find your way

<div class="grid cards" markdown>

-   :material-rocket-launch: **Quickstart**

    ---

    Install, add a key, run a read-only task, and find the session.

    [:octicons-arrow-right-24: Start here](guides/getting-started.md)

-   :material-stairs: **Use cases, easy to hard**

    ---

    "I want to… → run this" recipes in seven levels, from reading code to
    teams of roles and handing sessions to Claude Code.

    [:octicons-arrow-right-24: Use cases](use-cases/index.md)

-   :material-console: **Command builder**

    ---

    Click through your goal, workspace, and agent to get a ready-to-run
    command.

    [:octicons-arrow-right-24: Build a command](guides/command-builder.md)

-   :material-lightbulb-on-outline: **How Garuda works**

    ---

    Runtimes, workspaces, profiles, modes, and sessions on one page.

    [:octicons-arrow-right-24: Concepts](guides/how-garuda-works.md)

-   :material-shield-check: **Safety and workspaces**

    ---

    What each control does and does not protect, and when to use Docker.

    [:octicons-arrow-right-24: Safety](guides/safety-and-workspaces.md)

-   :material-file-document-outline: **Cheat sheet**

    ---

    Every everyday command on one page, ready to copy.

    [:octicons-arrow-right-24: Cheat sheet](reference/cheat-sheet.md)

-   :material-format-list-checks: **Feature index**

    ---

    Every feature, what it gives you, and where it is demonstrated.

    [:octicons-arrow-right-24: All features](reference/features.md)

-   :material-account-group: **Teams of roles**

    ---

    A planner, coder and independent reviewer, each on the model you choose.

    [:octicons-arrow-right-24: Level 5](use-cases/teams.md)

</div>

## What you get

| Capability | Details |
|---|---|
| **Any model** | Any LiteLLM provider, with retries, streaming, reasoning settings, prompt caching, and cost accounting |
| **Real tools** | Shell, files, edits, search, PDFs and spreadsheets, web fetch, MCP servers, skills, subagents, hooks, and your own Python tools |
| **Your own agents** | Version 1 agent definitions that extend packaged ones, inspected with `garuda agent show`, with structured JSON output and reviewed memory notes |
| **Guardrails** | Permission rules, read-only and no-edits runs, approvals for risky actions (answerable from anywhere), and trust prompts for project-supplied code |
| **Isolation** | Docker or remote Docker workspaces for untrusted code, an OS sandbox that limits writes and network, and Git worktrees per session |
| **Verified, not claimed** | Acceptance checks you choose decide whether work is verified; the agent's own completion check is recorded separately |
| **Parallel work** | Background runs with a queue, per-runtime capacity, worktree sessions, and merges checked in Docker |
| **Teams of roles** | Roles with an exact harness and model, fallbacks, plan → build → review flows, independent reviews, and consults |
| **Observability** | A dashboard with sessions, approvals, usage and cost by model, provider limits, and setup diagnostics |
| **External harnesses** | Claude Code, Codex, Cursor, OpenCode, Pi, and Goose through ACP, with handoff and recovery |
| **Many interfaces** | CLI, interactive chat, web dashboard, YAML recipes, Python SDK, and a JSON-RPC service |

## Contributing

Read the [Architecture](ARCHITECTURE.md), the [Module map](MODULES.md), and the
[Development guide](development/development.md). Coding agents should start
with the
[contributor guide](https://github.com/Darshan2104/Garuda-openagent/blob/main/AGENTS.md).
Open work is in the [backlog](BACKLOG.md); dated designs and plans are under
**Project history**.
