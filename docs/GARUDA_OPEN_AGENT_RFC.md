# Garuda Open Agent — Harness RFC v0.1

**Status:** Draft  
**Project:** [Garuda-openagent](https://github.com/)  
**Last updated:** July 2026  
**Authors:** Garuda team  

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Design North Star](#2-design-north-star)
3. [Agent Landscape Overview](#3-agent-landscape-overview)
4. [Per-Agent Analysis](#4-per-agent-analysis)
5. [Common Harness Patterns](#5-common-harness-patterns)
6. [Best-of-Breed Synthesis](#6-best-of-breed-synthesis)
7. [Garuda Architecture](#7-garuda-architecture)
8. [Core Interfaces (RFC)](#8-core-interfaces-rfc)
9. [Built-in Agent Profiles](#9-built-in-agent-profiles)
10. [Context Management](#10-context-management)
11. [Tool System](#11-tool-system)
12. [Permission & Safety Model](#12-permission--safety-model)
13. [Workspace Abstraction](#13-workspace-abstraction)
14. [Interfaces & Deployment Modes](#14-interfaces--deployment-modes)
15. [Evaluation Layer (Separate from Core)](#15-evaluation-layer-separate-from-core)
16. [Module Layout](#16-module-layout)
17. [Implementation Roadmap](#17-implementation-roadmap)
18. [Success Criteria](#18-success-criteria)
19. [What Not to Build in Core](#19-what-not-to-build-in-core)
20. [Quick Reference Tables](#20-quick-reference-tables)
21. [References](#21-references)

---

## 1. Executive Summary

Garuda Open Agent is a **universal, provider-agnostic agent harness** designed to run any language model against any terminal-based or software-engineering task. It is not a collection of domain-specific sub-agents. It is a **runtime** that combines the strongest ideas from the current open-agent ecosystem into one robust, configurable, and debuggable system.

**Core thesis:**

> The harness is the product, not the model. Small scaffolding improvements (context discipline, verification gates, execution optimization) often matter more than which frontier model you call.

**What Garuda combines:**

| Source | Contribution |
|--------|--------------|
| mini-SWE-agent | Protocol-based skeleton, linear history, bash-first simplicity |
| Terminus-2 | Out-of-sandbox execution, tmux mono-tool for real terminals |
| Terminus-KIRA | Native tool calling, marker polling, completion verification |
| OpenCode | Config-driven agents, permissions, MCP, plugin hooks |
| Goose | Recipes, MCP-native extensions, ACP-style auth |
| OpenHands SDK | Workspace abstraction, event-sourced state, typed tools |
| Codex CLI | Layered sandbox, compaction, JSON-RPC app-server |
| LucidCoder / Purple / Amadeus | Plan → execute → verify discipline (optional rigorous mode) |
| Harbor | Evaluation adapter, ATIF trajectories, parallel sandboxes |

**Evaluation note:** Spreadsheet and PDF benchmarks mentioned in the project README are **evaluation targets only**. They validate generality; they do not define the core product architecture.

---

## 2. Design North Star

### Goals

1. **Any model provider** — Anthropic, OpenAI, Google, local models via LiteLLM and native tool APIs
2. **Any terminal task** — from one-shot shell commands to interactive TUIs, servers, and pagers
3. **Better context management** — long-horizon tasks without context death
4. **Any use case via config** — new workflows through agent profiles, recipes, and MCP, not core forks
5. **Robust by default** — permissions, verification, sandboxing, full trajectory replay

### Non-Goals (v1)

- Shipping 50+ bespoke tools in core
- Building a full GUI like OpenHands web app
- Embedding eval-specific sub-agents (spreadsheet, PDF) into product logic
- Model-vendor lock-in or model-specific prompt hacks in core

### Guiding Principles

1. **Simple by default, rigorous when needed** — daily dev uses a lean loop; hard tasks opt into planner/critic mode
2. **Bash as primitive, not prison** — stateless subprocess for speed; tmux for interactive realism
3. **Config over code** — agents, permissions, recipes as files
4. **Eval plugs in, never defines** — Harbor/ATIF live in `eval/`, not `core/`
5. **Linear, auditable history** — trajectory equals message history unless subagent isolation is explicit

---

## 3. Agent Landscape Overview

Three layers exist in the ecosystem. Garuda targets the **middle layer**.

| Layer | Role | Examples |
|-------|------|----------|
| **Evaluation harness** | Run agents in sandboxes, score tasks, log trajectories | Harbor, Terminal-Bench |
| **Agent harness / runtime** | Prompt → model → tools → observe → repeat | OpenCode, Goose, Codex, Terminus-2, Garuda |
| **Shell / UX** | Terminal UI, workspace management | Agent Terminal, OpenHands GUI |

### Master Comparison Table

| Agent | Type | Primary Use | Tools | Model Lock-in | Autonomy | Terminal-Bench Role |
|-------|------|-------------|-------|---------------|----------|---------------------|
| [OpenCode](https://github.com/anomalyco/opencode) | Harness | Daily coding | Rich + MCP + LSP | None | Configurable | Supported via Harbor |
| [Goose](https://github.com/aaif-goose/goose) | Harness | Workflows + automation | MCP extensions | None | Modes (auto/approve) | General purpose |
| [OpenHands](https://github.com/OpenHands/OpenHands) | Platform | Production agents | Bash, files, browser, Jupyter | None | Configurable | Leaderboard presence |
| [Codex CLI](https://github.com/openai/codex) | Harness | IDE-integrated coding | Rich + MCP | OpenAI-optimized | Tiered approval | Supported via Harbor |
| [Terminus-2](https://github.com/harbor-framework/harbor) | Eval reference | Fair model comparison | tmux only | None | Full auto | Neutral testbed |
| [Terminus-KIRA](https://github.com/krafton-ai/KIRA) | Eval harness | Max TB score | tmux + native tools | None | Full auto | +~10pp over T-2 |
| [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent) | Harness | Baseline + RL | bash only | None | Full auto | Competitive |
| [SWE-agent](https://github.com/SWE-agent/SWE-agent) | Research (legacy) | Custom ACI experiments | Rich ACI | None | Configurable | Similar domain |
| [OpenHands-CLI](https://github.com/OpenHands/OpenHands-CLI) | CLI wrapper | Terminal access to SDK | SDK tools | None | Headless option | SDK consumer |
| [Agent Terminal](https://github.com/DaniAkash/agent-terminal) | Terminal UX | Multi-agent workspace | None (shell) | N/A | Human-driven | Runs other agents |
| [Harbor](https://github.com/harbor-framework/harbor) | Eval framework | Benchmarking any agent | N/A | N/A | N/A | Official harness |
| [LucidCoder](https://github.com/MDadopoulos/LucidCoder) | TB competition | Hard terminal tasks | Shell + state machine | Varies | Full auto | 4-stage pipeline |
| [Amadeus](https://agentbeats.dev/Desalzes/amadeus) | TB competition | Terminal engineer | Shell + critic | Provider-agnostic | Full auto | Adversarial critic |
| [AgentWhetters](https://github.com/paulwhitten/AgentWhetters-dispatch-general-purple) | Multi-benchmark | Skill dispatch | Per-skill handlers | Varies | Full auto | One of many skills |
| [Purple Terminal Agent](https://github.com/soutrikmachine/purple-terminal-agent) | TB competition | MoMS terminal agent | REPL + planner + RAG | Multi-model | Full auto | Planner + critic + RAG |

---

## 4. Per-Agent Analysis

### 4.1 OpenCode

**Repository:** https://github.com/anomalyco/opencode  
**License:** MIT  

#### Capabilities

- **Primary agents:** `build` (full access), `plan` (read-only analysis)
- **Subagents:** `general`, `explore` (read-only codebase search), `scout` (external docs/deps)
- **75+ LLM providers** via Models.dev
- **Tools:** file I/O, bash, LSP, MCP (stdio/HTTP/SSE)
- **Skills:** `SKILL.md` with frontmatter
- **Plugins:** `@opencode-ai/plugin` SDK with lifecycle hooks
- **Permissions:** per-tool/path/domain `allow | deny | ask`
- **Interfaces:** TUI, desktop (Tauri), HTTP API, IDE extension

#### Secret Sauce

1. **Everything is config-as-markdown** — drop agents in `.opencode/agents/`
2. **Provider freedom with subscription OAuth** — Copilot, ChatGPT accounts
3. **Agent topology as product** — Tab-switch primaries, `@mention` subagents, background subagents
4. **Deep plugin hook system** — intercept without forking core
5. **Privacy-first local execution**

#### Take for Garuda

Agent profiles as markdown, granular permissions, MCP-first extensibility, subagent isolation for explore/plan tasks.

---

### 4.2 Goose

**Repository:** https://github.com/aaif-goose/goose  
**License:** Apache 2.0  
**Governance:** Linux Foundation Agentic AI Foundation (AAIF)

#### Capabilities

- **15+ providers** including local Ollama
- **ACP (Agent Client Protocol)** — use Claude/ChatGPT/Gemini subscriptions
- **70+ MCP extensions**
- **Built-in extensions:** developer, memory, computercontroller, autovisualiser
- **Recipes:** portable YAML workflows with parameters and sub-recipes
- **Subagents:** parallel independent workers
- **Security:** prompt-injection detection, sandbox mode, adversary reviewer
- **Interfaces:** CLI, desktop, embeddable API (Rust)

#### Secret Sauce

1. **MCP-native from day one**
2. **Recipes as portable workflows** — team sharing and CI/CD
3. **Foundation governance** — neutral "browser of agents" positioning
4. **Rust performance and cross-platform core**
5. **ACP for subscription reuse**

#### Take for Garuda

Recipes (YAML workflows), MCP extension catalog mindset, ACP-style auth in v1.5.

---

### 4.3 OpenHands

**Repository:** https://github.com/OpenHands/OpenHands  
**License:** MIT  

#### Capabilities

- **Software Agent SDK** with 9 components: event state, LLM, tools, agent, context, conversation, secrets, security, deployment
- **Tools:** bash, file editor, task tracker, browser (BrowserGym), Jupyter, MCP
- **Workspaces:** Local, Docker, Remote (K8s-ready)
- **Multi-agent delegation**
- **Interfaces:** web GUI (VSCode-in-browser, VNC, Chromium), CLI, REST/WebSocket
- **Benchmarks:** SWE-bench, Terminal-Bench, WebArena, GAIA

#### Secret Sauce

1. **SDK as source of truth** — all interfaces consume same APIs
2. **Event-sourced deterministic replay**
3. **Workspace abstraction** — local → cloud with minimal code change
4. **Full environment stack** — terminal + browser + Jupyter
5. **Research-to-production bridge**

#### Take for Garuda

Workspace abstraction, event-sourced conversation log, typed tool schemas, secret registry. Do **not** copy full GUI in v1.

---

### 4.4 Codex CLI

**Repository:** https://github.com/openai/codex  
**Stack:** ~95% Rust (`codex-rs` workspace)

#### Capabilities

- **Agent loop** in `codex-core`: turns, tool dispatch, compaction
- **Tools:** shell, file patches, MCP client + MCP server (bidirectional)
- **Modes:** TUI, `codex exec` (headless), `codex app-server` (JSON-RPC)
- **Sandboxing:** Bubblewrap (Linux), Seatbelt (macOS)
- **Context:** auto-compaction, token-aware truncation (head + tail preserve)
- **Custom agents:** TOML (`~/.codex/config.toml`)
- **Parallel tool execution** with ordered results

#### Secret Sauce

1. **Rust rewrite for production hardening**
2. **Protocol-first** — `codex-protocol` decouples core from frontends
3. **Guardian module** — layered safety before execution
4. **Compaction as first-class API** with opaque state preservation
5. **Dual MCP role** — client and server

#### Take for Garuda

Layered approval policy, OS sandbox integration (v1.5), JSON-RPC IDE server (v1.5), smart output truncation, parallel safe tool calls.

---

### 4.5 Terminus-2

**Repository:** https://github.com/harbor-framework/harbor (agent implementation)  
**Docs:** https://www.harborframework.com/docs/agents/terminus-2

#### Capabilities

- **Single tool:** interactive tmux session
- **Agent runs outside container** — Python process, remote-capable
- **Full autonomy** — never asks for human input
- **LiteLLM backend** — any model
- **3-step summarization subagents:** Summary → Question → Answer
- **Proactive + reactive context recovery**
- **ATIF trajectory logging**

#### Secret Sauce

1. **Mono-tool philosophy** — no tool-schema bias across models
2. **Out-of-environment execution** — works when sandbox is broken
3. **Model-neutral by design**
4. **Quality summarization pipeline**
5. **Harbor's reference ruler**

#### Take for Garuda

tmux execution engine, out-of-sandbox agent process, 3-step summarization, proactive context threshold.

---

### 4.6 Terminus-KIRA

**Repository:** https://github.com/krafton-ai/KIRA  
**License:** Apache 2.0  

#### Capabilities

- **Native tool calling:** `execute_commands`, `task_complete`, `image_read`
- **Multimodal** image analysis from terminal tasks
- **Marker-based polling** — `echo '__CMDEND__<seq>__'`
- **Double-confirmation completion** — multi-perspective QA checklist
- **30KB output cap**
- **Anthropic prompt caching**

#### Reported Gains

~+10 percentage points on Terminal-Bench vs Terminus-2 across frontier models.

#### Secret Sauce

1. **Harness > model** — minimal changes, large score gains
2. **Native tools over ICL JSON parsing**
3. **Self-critique before submit**
4. **Pull-based tmux polling**
5. **"Final submission" prompt discipline**

#### Take for Garuda

Native tool calling, marker polling, output caps, completion verifier, multimodal read tool.

---

### 4.7 mini-SWE-agent

**Repository:** https://github.com/SWE-agent/mini-swe-agent  
**License:** MIT  

#### Capabilities

- **Bash only** — no tool-calling API required
- **Linear message history** — trajectory == prompt history
- **Stateless execution** — `subprocess.run` per action
- **Protocol-based design** — Agent / Model / Environment protocols
- **Environments:** local, Docker, Podman, Singularity, bubblewrap
- **LiteLLM / OpenRouter / Portkey**

#### Performance

>74% on SWE-bench Verified; competitive on Terminal-Bench.

#### Secret Sauce

1. **Radical minimalism** — ~100 lines for core agent class
2. **No persistent shell session** — stable, parallelizable, sandbox-trivial
3. **LM does everything via bash**
4. **Anti-overfitting baseline for RL/FT**
5. **Industry adoption** (Meta, NVIDIA, Princeton, Stanford)

#### Take for Garuda

Foundation architecture: protocols, linear history, bash as default primitive, stateless subprocess engine.

---

### 4.8 SWE-agent (maintenance mode)

**Repository:** https://github.com/SWE-agent/SWE-agent  
**Status:** Superseded by mini-SWE-agent for new work  

#### Capabilities

- **Agent-Computer Interface (ACI):** custom file viewer, editor, search, linter on edits
- **Docker via SWE-ReX** — local or remote
- **YAML-configured** agents and history processors
- **EnIGMA mode** — cybersecurity/CTF
- **GitHub issue → patch** workflow

#### Secret Sauce

1. **ACI design thesis** — good tool interfaces beat raw bash for LMs
2. **Linter-gated edits**
3. **Purpose-built file viewer** (100-line windows)
4. **History processors**
5. **Pioneered agent scaffold research** (NeurIPS 2024)

#### Take for Garuda

Optional rich file tools (read with line ranges, patch with lint check) as **tools**, not as the only execution model. History processor hooks.

---

### 4.9 OpenHands-CLI

**Repository:** https://github.com/OpenHands/OpenHands-CLI  

#### Capabilities

- **Modes:** TUI, headless, web TUI, `serve` (full GUI), ACP for IDEs
- **Model-agnostic** via LiteLLM
- **PyInstaller binary** — no Docker required for CLI
- **JSONL event stream** for CI

#### Secret Sauce

Thin SDK-powered UX layer; multiple deployment surfaces from one package.

#### Take for Garuda

Headless-first CLI design, binary distribution goal (v1.5+), JSONL stream for automation.

---

### 4.10 Agent Terminal

**Repository:** https://github.com/DaniAkash/agent-terminal  
**Platform:** macOS  

#### Capabilities

- Project-scoped workspaces, persistent tabs/CWD
- Live status bar: PID, ports, git, model, memory
- Agent detection for Claude Code, Codex
- Rust-native PTY, OSC 133 shell integration

#### Secret Sauce

Agent-aware terminal UX — not an agent policy.

#### Take for Garuda

Ensure CLI emits structured status events compatible with agent-aware terminals. No core dependency.

---

### 4.11 Harbor Framework

**Repository:** https://github.com/harbor-framework/harbor  

#### Capabilities

- **Unified agent interface:** `BaseAgent`, `BaseInstalledAgent`
- **Pre-integrated agents:** Terminus-2, Claude Code, Codex, OpenHands, Mini-SWE-Agent, etc.
- **Sandbox providers:** Docker, E2B, Modal, Daytona
- **ATIF** — Agent Trajectory Interchange Format
- **Parallel eval:** 32–100 containers
- **Custom agents:** `--agent-import-path`

#### Secret Sauce

1. Agent/environment decoupling
2. ATIF as cross-agent trajectory standard
3. Scale-native parallel rollouts
4. Registry model: `harbor run -d terminal-bench@2.0`

#### Take for Garuda

`eval/harbor_adapter.py` implementing `BaseAgent` + ATIF export. Never merge eval logic into core loop.

---

### 4.12 Community Terminal-Bench Agents

#### LucidCoder

**Repository:** https://github.com/MDadopoulos/LucidCoder  

- **Pipeline:** decompose → plan (checker) → execute (retry + anti-paralysis) → verify
- **Protocol:** A2A + `terminal-bench-shell-v1`
- **Secret sauce:** strict state machine, verification-before-completion

#### Amadeus (Desalzes)

- **Pipeline:** explore → plan → execute → self-verify/repair
- **Adversarial critic** pre-flights commands
- **Secret sauce:** critic catches interactive hangs and blind pattern-copying

#### AgentWhetters

**Repository:** https://github.com/paulwhitten/AgentWhetters-dispatch-general-purple  

- **Multi-benchmark dispatch** — structural + LLM classifier routes to skill handlers
- **Secret sauce:** one agent, many benchmarks via skill router

#### Purple Terminal Agent

**Repository:** https://github.com/soutrikmachine/purple-terminal-agent  

- **MoMS:** mixture of models and specialists
- **Yielding REPL heartbeat** — solves A2A 60s timeout
- **Hierarchical planner + critic + domain critics + TF-IDF RAG**
- **Secret sauce:** inference-time depth scaling + oracle-task RAG

#### Apex2

**Repository:** https://github.com/heartyguy/Apex2-Terminal-Bench-Agent  

- **Strategic simplification** — less complexity, SOTA via research loop + prompting
- **Secret sauce:** sophisticated web search/research before execution

#### Take for Garuda

Optional `rigorous` mode: planner state machine, critic pre-flight, anti-paralysis guard. Keep off by default.

---

## 5. Common Harness Patterns

Every production agent harness shares these primitives:

### 5.1 Core Loop

```
User Task
  → Build Prompt + Context
  → LLM Inference
  → Tool Call? ──Yes──→ Execute Tool → Observe → (loop)
              └──No───→ Final Response / Done
```

### 5.2 Shared Component Matrix

| Component | Purpose | Present In |
|-----------|---------|------------|
| Agent loop | Turn-by-turn orchestration | All |
| Tool registry | Define model capabilities | All |
| Environment abstraction | Isolate execution | All |
| LLM provider layer | Model-agnostic inference | All modern harnesses |
| Message/trajectory history | Audit, debug, FT/RL | All |
| Context window management | Survive long tasks | Production agents |
| Permission system | Safety before destructive ops | OpenCode, Codex, Goose, OpenHands |
| MCP integration | Portable tool extensions | OpenCode, Goose, Codex, OpenHands |
| Subagents / delegation | Parallelism + specialization | OpenCode, Goose, OpenHands, Terminus-2 |
| Headless / API mode | CI, IDE, automation | Codex, OpenHands-CLI, OpenCode |
| Sandboxing | OS-level isolation | Codex, OpenHands, Harbor eval |
| Config-as-files | Customize without code forks | OpenCode, Goose, Codex, SWE-agent |
| Structured trajectories | Benchmarking + training | Harbor/ATIF, mini-SWE-agent |

### 5.3 Two Philosophical Forks

| Approach | Advocates | Tradeoff |
|----------|-----------|----------|
| **Rich tool surface** | SWE-agent, OpenHands, OpenCode | Higher capability; more tuning per model |
| **Minimal tool surface** | Terminus-2, mini-SWE-agent | Model-neutral; fair benchmarks |
| **Harness tricks** | KIRA, LucidCoder, Purple | Big benchmark gains; more complexity |

**Garuda position:** Rich tools available, bash+tmux as defaults, harness tricks in optional `rigorous` mode.

### 5.4 Ranked Impact Patterns

1. Good context management (summarization, caps, compaction)
2. Completion verification (don't trust "I'm done")
3. Provider abstraction (LiteLLM + native tool fallback)
4. MCP as extension port
5. Permission granularity
6. Subagent context isolation
7. Execution optimization (marker polling, stateless subprocess)
8. Config-driven customization
9. Trajectory standardization (ATIF)
10. Sandbox outside agent process

---

## 6. Best-of-Breed Synthesis

### The Garuda Formula

```
mini-SWE-agent skeleton
+ Terminus tmux realism
+ KIRA execution/verification discipline
+ OpenCode configurability
+ Goose recipes/MCP
+ OpenHands workspace model
+ Codex safety/interfaces
+ (optional) LucidCoder/Purple rigorous mode
```

### Dual Execution Engine (Critical Design Decision)

| Engine | Mechanism | When |
|--------|-----------|------|
| **Stateless bash** | `subprocess.run` / `docker exec` | Fast commands, parallel ops, sandbox eval |
| **Stateful tmux** | Persistent tmux session | Interactive TUIs, pagers, menus, servers |

**Rule:** Default stateless. Escalate to tmux when interactive or stateful behavior is detected or requested.

### Ten Non-Negotiable Capabilities

1. Dual execution engine (bash + tmux)
2. Context management stack (caps, polling, summarization, subagent handoff)
3. Native tool calling + text fallback
4. Completion verification gate
5. Permission system (allow/deny/ask)
6. Config-driven agent profiles
7. MCP-first extensibility
8. Recipes for repeatable workflows
9. Workspace portability (local → docker → remote)
10. Observability + ATIF replay

---

## 7. Garuda Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Garuda Open Agent                        │
├─────────────────────────────────────────────────────────────┤
│  Interfaces                                                  │
│    CLI/TUI  │  Headless API  │  IDE Server (v1.5)           │
├─────────────────────────────────────────────────────────────┤
│  Core Harness                                                │
│    AgentLoop │ ContextManager │ PermissionEngine             │
│    CompletionVerifier │ EventStore                           │
├─────────────────────────────────────────────────────────────┤
│  Agent Profiles (config)                                     │
│    build │ plan │ explore │ [user-defined]                   │
├─────────────────────────────────────────────────────────────┤
│  Model Layer                                                 │
│    LiteLLM │ Native Tools API │ Text Parser Fallback         │
│    API Key + ACP/OAuth (v1.5)                                │
├─────────────────────────────────────────────────────────────┤
│  Tool Layer                                                  │
│    bash │ tmux │ read │ write │ patch │ task_complete        │
│    image_read │ MCP Client                                   │
├─────────────────────────────────────────────────────────────┤
│  Workspace Layer                                             │
│    LocalWorkspace │ DockerWorkspace │ RemoteWorkspace (v2)   │
├─────────────────────────────────────────────────────────────┤
│  Eval Adapter (separate package)                             │
│    Harbor BaseAgent │ ATIF Export │ Benchmark Tasks          │
└─────────────────────────────────────────────────────────────┘
```

---

## 8. Core Interfaces (RFC)

All interfaces use **structural subtyping** (Python `Protocol`) — no inheritance required. Inspired by mini-SWE-agent.

### 8.1 Message Types

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class Message:
    role: Role
    content: str
    name: str | None = None          # tool name when role=TOOL
    tool_call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 8.2 Model Protocol

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class Model(Protocol):
  """Provider-agnostic LLM interface."""

  @property
  def model_name(self) -> str: ...

  @property
  def supports_tool_calling(self) -> bool: ...

  async def complete(
      self,
      messages: list[Message],
      tools: list[dict] | None = None,
      temperature: float | None = None,
      max_tokens: int | None = None,
  ) -> "ModelResponse": ...

  def count_tokens(self, messages: list[Message]) -> int: ...


@dataclass
class ModelResponse:
  content: str | None
  tool_calls: list[ToolCall]
  raw: dict[str, Any] = field(default_factory=dict)
  usage: dict[str, int] = field(default_factory=dict)
```

**Implementations:**

- `LitellmModel` — primary; all providers via LiteLLM
- `ResponsesApiModel` — OpenAI Responses API with compaction support
- `TextParserModel` — wraps completion-only models; parses action from text

### 8.3 Environment Protocol

```python
@runtime_checkable
class Environment(Protocol):
  """Execution surface for tools."""

  async def execute(
      self,
      command: str,
      timeout: float | None = None,
      cwd: str | None = None,
  ) -> "ExecResult": ...

  async def read_file(self, path: str) -> str: ...

  async def write_file(self, path: str, content: str) -> None: ...

  @property
  def workspace_root(self) -> str: ...


@dataclass
class ExecResult:
  stdout: str
  stderr: str
  exit_code: int
  duration_ms: int
  truncated: bool = False
```

**Implementations:**

- `LocalEnvironment` — `subprocess.run` on host
- `DockerEnvironment` — `docker exec` per command (stateless)
- `TmuxEnvironment` — persistent tmux session inside container/host
- `DockerTmuxEnvironment` — tmux inside Docker (Terminus-style)

### 8.4 Tool Protocol

```python
@runtime_checkable
class Tool(Protocol):
  name: str
  description: str
  parameters: dict  # JSON Schema

  async def execute(
      self,
      arguments: dict,
      env: Environment,
      ctx: "ToolContext",
  ) -> ToolResult: ...


@dataclass
class ToolContext:
  session_id: str
  agent_profile: str
  permissions: "PermissionEngine"
  events: "EventStore"
```

**Built-in tools (v1):**

| Tool | Description |
|------|-------------|
| `bash` | Stateless command execution |
| `tmux_send` | Send keys to tmux session |
| `tmux_capture` | Read tmux pane output |
| `read_file` | Read file with optional line range |
| `write_file` | Write/create file |
| `apply_patch` | Unified diff patch application |
| `image_read` | Base64 multimodal image analysis |
| `task_complete` | Signal completion (triggers verifier) |
| `mcp_call` | Delegate to MCP server tool |

### 8.5 Agent Protocol

```python
@runtime_checkable
class Agent(Protocol):
  """Orchestrates the reasoning-action loop."""

  @property
  def profile_name(self) -> str: ...

  async def run(
      self,
      task: str,
      model: Model,
      env: Environment,
      tools: list[Tool],
      config: "AgentConfig",
  ) -> "AgentResult": ...


@dataclass
class AgentConfig:
  max_turns: int = 200
  mode: str = "standard"           # standard | rigorous | readonly
  permission_mode: str = "smart"   # auto | smart | readonly | yolo
  proactive_summarize_threshold: int = 8000
  max_output_bytes: int = 30_720   # 30KB per observation
  enable_verifier: bool = True
  enable_tmux: bool = True
  marker_polling: bool = True
  system_prompt: str | None = None
  allowed_tools: list[str] | None = None


@dataclass
class AgentResult:
  success: bool
  final_message: str
  messages: list[Message]
  turns: int
  metadata: dict[str, Any] = field(default_factory=dict)
```

**Implementations:**

- `DefaultAgent` — standard ReAct loop (mini-SWE-agent style)
- `RigorousAgent` — plan → execute → verify state machine (v1.5)

### 8.6 Context Manager

```python
class ContextManager:
  """Manages conversation history within token budget."""

  def __init__(
      self,
      model: Model,
      max_tokens: int,
      max_output_bytes: int = 30_720,
      proactive_threshold: int = 8000,
  ): ...

  def append(self, message: Message) -> None: ...

  def get_messages(self) -> list[Message]: ...

  async def maybe_summarize(self) -> bool:
    """Proactive summarization when free tokens < threshold."""

  async def summarize_on_overflow(self) -> None:
    """Reactive 3-step summarization on context overflow."""

  def shape_observation(self, output: str) -> str:
    """Cap size, preserve head+tail, handle empty output."""

  def fork_for_subagent(self) -> "ContextManager":
    """Isolated context for explore/plan subagents."""
```

**3-step summarization (from Terminus-2):**

1. **Summary subagent** — compress full history
2. **Question subagent** — identify gaps in summary
3. **Answer subagent** — fill gaps from history

Main agent continues with: system prompt + task + summary + Q&A.

### 8.7 Completion Verifier

```python
class CompletionVerifier:
  """Gate before accepting task_complete. Inspired by Terminus-KIRA."""

  async def verify(
      self,
      task: str,
      messages: list[Message],
      env: Environment,
      model: Model,
  ) -> "VerificationResult": ...


@dataclass
class VerificationResult:
  approved: bool
  checklist: dict[str, bool]   # requirements, tests, robustness, artifacts
  feedback: str | None         # injected back into loop if rejected
```

**Checklist perspectives (lightweight, single model call):**

1. **Builder** — original requirements met?
2. **Tester** — verification commands run and pass?
3. **Reviewer** — edge cases, robustness, no premature completion?

### 8.8 Permission Engine

```python
class PermissionDecision(str, Enum):
  ALLOW = "allow"
  DENY = "deny"
  ASK = "ask"


class PermissionEngine:
  def check_tool(self, tool_name: str) -> PermissionDecision: ...

  def check_path(self, path: str, operation: str) -> PermissionDecision: ...

  def check_command(self, command: str) -> PermissionDecision: ...

  async def request_approval(self, action: str, reason: str) -> bool: ...
```

**Config source:** agent profile YAML + `.garuda/permissions.yaml`

### 8.9 Event Store

```python
class EventType(str, Enum):
  USER_MESSAGE = "user_message"
  MODEL_RESPONSE = "model_response"
  TOOL_CALL = "tool_call"
  TOOL_RESULT = "tool_result"
  PERMISSION_ASK = "permission_ask"
  SUMMARIZATION = "summarization"
  VERIFICATION = "verification"
  SESSION_START = "session_start"
  SESSION_END = "session_end"


@dataclass
class Event:
  type: EventType
  timestamp: str
  payload: dict[str, Any]


class EventStore:
  def append(self, event: Event) -> None: ...

  def get_all(self) -> list[Event]: ...

  def to_atif(self) -> dict:
    """Export Harbor ATIF-compatible trajectory."""

  def save(self, path: str) -> None: ...

  @classmethod
  def load(cls, path: str) -> "EventStore": ...
```

### 8.10 Workspace Protocol

```python
@runtime_checkable
class Workspace(Protocol):
  """Lifecycle manager for execution environments."""

  async def start(self) -> None: ...

  async def stop(self) -> None: ...

  def get_environment(self) -> Environment: ...

  @property
  def is_sandboxed(self) -> bool: ...
```

**Implementations:**

- `LocalWorkspace` — direct host execution
- `DockerWorkspace` — container per session
- `RemoteWorkspace` — API-spawned container (v2)

### 8.11 Agent Profile Loader

Agent profiles are **markdown or YAML files** with frontmatter:

```yaml
# .garuda/agents/build.yaml
name: build
description: Full-access agent for implementation work
model: anthropic/claude-sonnet-4-20250514
mode: standard
permission_mode: smart
tools:
  - bash
  - tmux_send
  - tmux_capture
  - read_file
  - write_file
  - apply_patch
  - image_read
  - task_complete
  - mcp_call
permissions:
  bash:
    default: ask
    patterns:
      - pattern: "rm -rf /"
        decision: deny
      - pattern: "git status"
        decision: allow
system_prompt: |
  You are a capable software engineering agent...
```

```yaml
# .garuda/agents/plan.yaml
name: plan
description: Read-only analysis and planning
mode: readonly
permission_mode: readonly
tools:
  - bash
  - read_file
permissions:
  write_file: deny
  apply_patch: deny
  bash:
    default: allow
    patterns:
      - pattern: "rm "
        decision: deny
```

```yaml
# .garuda/agents/explore.yaml
name: explore
description: Fast codebase exploration (subagent)
mode: readonly
tools:
  - bash
  - read_file
subagent: true
system_prompt: |
  You are a fast, read-only exploration agent...
```

### 8.12 Recipe Format (v1.5)

```yaml
# .garuda/recipes/fix-and-test.yaml
name: fix-and-test
description: Fix a bug and run tests
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
    prompt: "Run {{test_command}} and fix any failures"
```

---

## 9. Built-in Agent Profiles

| Profile | Access | Tools | Use Case |
|---------|--------|-------|----------|
| **build** | Read/write/exec | All core + MCP | Implementation, fixes, deployment |
| **plan** | Read-only | bash (safe), read_file | Analysis, architecture, planning |
| **explore** | Read-only | bash (safe), read_file | Fast codebase search (subagent) |

Users invoke subagents via `@explore` mention or programmatic delegation. Subagent returns distilled summary to parent context.

---

## 10. Context Management

### Layer Stack (apply in order)

| # | Layer | Source | Behavior |
|---|-------|--------|----------|
| 1 | Output shaping | KIRA | Cap at 30KB; preserve head + tail; meaningful empty-output message |
| 2 | Marker polling | KIRA | Append `echo '__CMDEND__<seq>__'`; proceed when marker seen |
| 3 | Proactive summarize | Terminus-2 | Trigger when free tokens < threshold (default 8000) |
| 4 | 3-step summarize | Terminus-2 | Summary → Questions → Answers |
| 5 | Compaction API | Codex | Use provider `/compact` when available |
| 6 | Subagent isolation | OpenCode | Explore/search in forked context; return summary only |
| 7 | Linear primary history | mini-SWE-agent | Main thread stays append-only and auditable |

### Subagent Handoff Pattern

```
Parent agent
  → spawns @explore with isolated ContextManager
  → explore runs N turns in fork
  → returns structured summary (files, patterns, findings)
  → parent appends single USER message with summary
  → parent context stays clean
```

---

## 11. Tool System

### Execution Routing

```python
async def route_execution(command: str, env: Environment, config: AgentConfig) -> ExecResult:
  if config.enable_tmux and needs_tmux(command):
    return await env.execute_in_tmux(command, marker=config.marker_polling)
  return await env.execute(command)
```

**`needs_tmux` heuristics:**

- User explicitly requests tmux mode
- Command launches interactive TUI (detected or flagged)
- Previous command left background process requiring follow-up input
- Pager detected in output (`--More--`, `(END)`)

### Marker Polling (KIRA)

```bash
# Agent sends:
./long_running_script.sh ; echo '__CMDEND__42__'

# Harness polls tmux buffer; if marker appears before timeout, proceed immediately
```

### MCP Integration

```yaml
# .garuda/mcp.yaml
servers:
  - name: github
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"
```

Tools from MCP servers are namespaced: `mcp__github__create_issue`.

---

## 12. Permission & Safety Model

### Modes

| Mode | Behavior |
|------|----------|
| `auto` | Allow all (dev only) |
| `smart` | **Default.** allow safe, ask risky, deny destructive |
| `readonly` | No writes, no destructive bash |
| `yolo` | Allow all in sandboxed eval only |

### Layered Checks (Codex-inspired)

```
1. Plugin hooks (pre-tool)
2. Permission engine (allow/deny/ask)
3. Critic pre-flight (rigorous mode only)
4. OS sandbox (bubblewrap/seatbelt, v1.5)
5. User approval (if ask)
6. Execute
7. Plugin hooks (post-tool)
```

### Default Deny Patterns

- `rm -rf /`, `mkfs`, `dd if=`
- Writing outside workspace root
- Network exfil patterns (configurable)

---

## 13. Workspace Abstraction

| Workspace | Execution | Sandbox | Use Case |
|-----------|-----------|---------|----------|
| `LocalWorkspace` | Host subprocess/tmux | None | Daily development |
| `DockerWorkspace` | Container subprocess/tmux | Docker | Eval, untrusted tasks |
| `RemoteWorkspace` | API-spawned container | Cloud | Scale, CI (v2) |

**Key invariant:** `Agent.run()` receives `Environment`, not raw Docker APIs. Same agent code everywhere.

---

## 14. Interfaces & Deployment Modes

| Mode | Command | Use Case |
|------|---------|----------|
| **Interactive CLI** | `garuda` | Daily terminal development |
| **Headless** | `garuda run --headless -t "task"` | CI, scripts, automation |
| **JSONL stream** | `garuda run --headless --json` | Agent-driving-agent |
| **IDE server** | `garuda serve` (v1.5) | JSON-RPC for IDE extensions |
| **Recipe** | `garuda recipe fix-and-test --issue "..."` | Repeatable workflows (v1.5) |

### CLI Flags (Headless)

| Flag | Effect |
|------|--------|
| `--headless` | No TUI; requires `-t` or `-f` |
| `--json` | JSONL event stream |
| `-t TEXT` | Task prompt |
| `-f PATH` | Task from file |
| `--agent NAME` | Agent profile (build/plan/custom) |
| `--model NAME` | Override model |
| `--workspace docker` | Use DockerWorkspace |
| `--mode rigorous` | Enable planner + critic |
| `--permission-mode smart` | Permission behavior |
| `--resume ID` | Resume session |

---

## 15. Evaluation Layer (Separate from Core)

```
garuda/eval/
├── harbor_adapter.py    # implements Harbor BaseAgent
├── atif_export.py       # EventStore → ATIF JSON
└── benchmarks/
    ├── terminal_bench/  # Harbor run configs
    ├── spreadsheet/     # SpreadsheetBench tasks (eval only)
    └── pdf/             # OfficeQA tasks (eval only)
```

### Harbor Integration

```python
class GarudaHarborAgent(BaseAgent):
  SUPPORTS_ATIF = True

  async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
    workspace = DockerTmuxWorkspace.from_harbor(environment)
    agent = DefaultAgent(profile="build")
    result = await agent.run(
      task=instruction,
      model=LitellmModel(self.model_name),
      env=workspace.get_environment(),
      tools=default_tools(),
      config=AgentConfig(mode="rigorous", permission_mode="yolo"),
    )
    context.rollout_details = result.metadata.get("atif_trajectory")
```

**Principle:** Benchmark-specific logic (spreadsheet validators, PDF parsers) lives in `eval/benchmarks/`, never in `garuda/core/`.

---

## 16. Module Layout

```
garuda/
├── __init__.py
├── core/
│   ├── loop.py              # DefaultAgent, RigorousAgent
│   ├── events.py            # EventStore, EventType
│   ├── permissions.py       # PermissionEngine
│   └── verifier.py          # CompletionVerifier
├── model/
│   ├── protocol.py          # Model, ModelResponse
│   ├── litellm_model.py
│   ├── responses_model.py
│   └── text_parser.py       # fallback for non-tool models
├── context/
│   ├── manager.py           # ContextManager
│   ├── summarizer.py        # 3-step summarization
│   └── shaper.py            # output caps, head/tail
├── tools/
│   ├── protocol.py          # Tool, ToolContext
│   ├── bash.py
│   ├── tmux.py
│   ├── files.py             # read, write, patch
│   ├── image_read.py
│   ├── task_complete.py
│   └── mcp.py
├── workspace/
│   ├── protocol.py          # Workspace
│   ├── local.py
│   ├── docker.py
│   └── tmux.py              # TmuxEnvironment, DockerTmuxEnvironment
├── agents/
│   ├── loader.py            # load .garuda/agents/*.yaml
│   └── defaults/            # build, plan, explore templates
├── config/
│   ├── settings.py          # global settings
│   └── recipes.py           # recipe loader (v1.5)
├── interfaces/
│   ├── cli.py               # interactive TUI
│   ├── headless.py          # automation entrypoint
│   └── server.py            # JSON-RPC (v1.5)
├── plugins/
│   └── hooks.py             # lifecycle hook registry
└── eval/                    # NOT imported by core
    ├── harbor_adapter.py
    ├── atif_export.py
    └── benchmarks/
```

---

## 17. Implementation Roadmap

### Phase 1 — Skeleton (MVP)

- [ ] `Model`, `Environment`, `Agent`, `Tool` protocols
- [ ] `LitellmModel` implementation
- [ ] `LocalEnvironment` with stateless bash
- [ ] `DefaultAgent` linear loop
- [ ] `read_file`, `write_file`, `bash` tools
- [ ] `EventStore` (append-only JSONL)
- [ ] Headless CLI: `garuda run --headless -t "..."`

**Exit criteria:** Run a simple coding task end-to-end on local machine with any LiteLLM model.

### Phase 2 — Reliability

- [ ] `ContextManager` with output shaping (30KB cap)
- [ ] `PermissionEngine` with allow/deny/ask
- [ ] Native tool calling + `TextParserModel` fallback
- [ ] `CompletionVerifier` with checklist gate
- [ ] `apply_patch` tool
- [ ] Agent profile loader (YAML)
- [ ] Interactive CLI (basic TUI)

**Exit criteria:** Multi-turn task with permission prompts and completion verification.

### Phase 3 — Terminal Realism

- [ ] `TmuxEnvironment` + `DockerTmuxEnvironment`
- [ ] Marker-based polling
- [ ] `tmux_send`, `tmux_capture` tools
- [ ] Execution routing (bash vs tmux)
- [ ] 3-step proactive summarization
- [ ] `image_read` multimodal tool

**Exit criteria:** Complete a Terminal-Bench-style interactive task (menu navigation, server startup).

### Phase 4 — Extensibility

- [ ] MCP client integration
- [ ] Plugin hook system
- [ ] `plan` and `explore` agent profiles
- [ ] Subagent handoff with context fork
- [ ] `DockerWorkspace`

**Exit criteria:** Custom agent profile + MCP tool used in Docker sandbox.

### Phase 5 — Evaluation

- [ ] Harbor `BaseAgent` adapter
- [ ] ATIF trajectory export
- [ ] `harbor run` configs for Terminal-Bench 2.0
- [ ] Benchmark harness for SpreadsheetBench (eval only)
- [ ] Benchmark harness for OfficeQA/PDF (eval only)

**Exit criteria:** Score on Terminal-Bench 2.0 via Harbor with ATIF logs.

### Phase 6 — Production (v1.5+)

- [ ] Recipes (YAML workflows)
- [ ] `RigorousAgent` (plan → execute → verify + critic)
- [ ] JSON-RPC IDE server
- [ ] OS sandbox (bubblewrap/seatbelt)
- [ ] ACP/OAuth provider auth
- [ ] PyInstaller binary distribution

---

## 18. Success Criteria

| Dimension | Target |
|-----------|--------|
| **Provider freedom** | Same task on Claude, GPT, Gemini, local model without code change |
| **Terminal realism** | Interactive TUIs, pagers, servers via tmux |
| **Long tasks** | 100+ turns without context death |
| **Safety** | Destructive ops gated; sandboxed eval mode |
| **Adaptability** | New use case = new agent YAML + optional MCP |
| **Debuggability** | Full trajectory replay; clear stop reason |
| **Benchmark-ready** | Harbor/Terminal-Bench without architecture changes |
| **Minimal core** | <2000 LOC for core loop + protocols (excluding tests) |

---

## 19. What Not to Build in Core

| Anti-pattern | Why |
|--------------|-----|
| 50+ bespoke tools | Overfits benchmarks; use MCP |
| Spreadsheet/PDF sub-agents in product | Eval-only; use benchmark verifiers |
| Full web GUI | Massive scope; defer to v2+ |
| Always-on multi-agent orchestration | Complexity; use `rigorous` mode only |
| Model-specific prompts in core | Defeats provider-agnostic goal |
| In-agent benchmark scoring | Belongs in Harbor eval layer |

---

## 20. Quick Reference Tables

### What to Borrow From Whom

| Capability | Primary Source | Garuda Module |
|------------|----------------|---------------|
| Protocol skeleton | mini-SWE-agent | `core/`, `model/`, `workspace/` |
| tmux execution | Terminus-2 | `workspace/tmux.py`, `tools/tmux.py` |
| Native tools + verification | Terminus-KIRA | `model/`, `core/verifier.py` |
| Agent profiles + permissions | OpenCode | `agents/`, `core/permissions.py` |
| Recipes | Goose | `config/recipes.py` |
| Workspace abstraction | OpenHands | `workspace/` |
| Sandbox + IDE protocol | Codex | `interfaces/server.py`, OS sandbox |
| Planner + critic | LucidCoder/Purple | `core/loop.py` (RigorousAgent) |
| ATIF + Harbor | Harbor | `eval/` |

### Agent Secret Sauce Summary

| Agent | #1 Secret |
|-------|-----------|
| OpenCode | Config-as-markdown + provider freedom |
| Goose | MCP-native + recipes |
| OpenHands | SDK workspace + event replay |
| Codex | Rust sandbox + protocol decoupling |
| Terminus-2 | Mono-tool tmux + out-of-sandbox |
| Terminus-KIRA | Native tools + completion checklist |
| mini-SWE-agent | 100-line loop + stateless bash |
| SWE-agent | ACI tool design |
| LucidCoder | 4-stage state machine |
| Purple | Planner + critic + RAG |
| Harbor | ATIF + agent/environment decoupling |

---

## 21. References

### Primary Repositories

| Project | URL |
|---------|-----|
| OpenCode | https://github.com/anomalyco/opencode |
| Goose | https://github.com/aaif-goose/goose |
| OpenHands | https://github.com/OpenHands/OpenHands |
| OpenHands SDK Docs | https://docs.openhands.dev/sdk/arch/overview |
| Codex CLI | https://github.com/openai/codex |
| Terminus-2 (Harbor) | https://www.harborframework.com/docs/agents/terminus-2 |
| Terminus-KIRA | https://github.com/krafton-ai/KIRA |
| mini-SWE-agent | https://github.com/SWE-agent/mini-swe-agent |
| SWE-agent | https://github.com/SWE-agent/SWE-agent |
| OpenHands-CLI | https://github.com/OpenHands/OpenHands-CLI |
| Agent Terminal | https://github.com/DaniAkash/agent-terminal |
| Harbor | https://github.com/harbor-framework/harbor |
| Terminal-Bench | https://www.tbench.ai/ |
| LucidCoder | https://github.com/MDadopoulos/LucidCoder |
| AgentWhetters | https://github.com/paulwhitten/AgentWhetters-dispatch-general-purple |
| Purple Terminal Agent | https://github.com/soutrikmachine/purple-terminal-agent |

### Papers & Docs

| Resource | URL |
|----------|-----|
| Terminal-Bench paper | https://arxiv.org/abs/2601.11868 |
| SWE-agent paper | https://arxiv.org/abs/2405.15793 |
| OpenHands SDK paper | https://arxiv.org/html/2511.03690v1 |
| ATIF specification | https://www.harborframework.com/docs/agents/trajectory-format |
| OpenCode agents docs | https://opencode.ai/docs/agents |
| Goose docs | https://goose-docs.ai/docs/quickstart |

### Benchmarks (Evaluation Only)

| Benchmark | URL |
|-----------|-----|
| Terminal-Bench 2.0 | https://www.tbench.ai/ |
| SpreadsheetBench | https://spreadsheetbench.github.io/ |
| SWE-bench | https://www.swebench.com/ |
| AgentBeats | https://agentbeats.dev/ |

---

## Appendix A: Default Agent Loop (Pseudocode)

```python
async def run_agent(task: str, model: Model, env: Environment, config: AgentConfig) -> AgentResult:
  ctx = ContextManager(model, max_tokens=model.context_limit)
  verifier = CompletionVerifier()
  permissions = PermissionEngine(config.permission_mode)
  events = EventStore()

  ctx.append(Message(role=Role.USER, content=task))
  events.append(Event(type=EventType.SESSION_START, ...))

  for turn in range(config.max_turns):
    await ctx.maybe_summarize()
    messages = ctx.get_messages()
    tools = get_tools_for_profile(config)
    response = await model.complete(messages, tools=tools_schema(tools))

    if response.tool_calls:
      for call in response.tool_calls:
        if call.name == "task_complete":
          result = await verifier.verify(task, ctx.get_messages(), env, model)
          if result.approved:
            return AgentResult(success=True, final_message=call.arguments["summary"], ...)
          ctx.append(Message(role=Role.USER, content=f"Verification failed: {result.feedback}"))
          continue

        decision = permissions.check(call)
        if decision == PermissionDecision.DENY:
          ctx.append(tool_error(call, "Permission denied"))
          continue
        if decision == PermissionDecision.ASK:
          if not await permissions.request_approval(call):
            continue

        tool_result = await execute_tool(call, env, config)
        shaped = ctx.shape_observation(tool_result.content)
        ctx.append(tool_result_message(call, shaped))
        events.append(Event(type=EventType.TOOL_RESULT, ...))
    else:
      ctx.append(Message(role=Role.ASSISTANT, content=response.content))

    if is_done(response):
      break

  return AgentResult(success=False, final_message="Max turns exceeded", ...)
```

---

## Appendix B: Glossary

| Term | Definition |
|------|------------|
| **Harness** | Runtime that orchestrates model + tools + environment |
| **Agent profile** | Config defining behavior, tools, permissions for one agent mode |
| **ACI** | Agent-Computer Interface (SWE-agent's tool design concept) |
| **ATIF** | Agent Trajectory Interchange Format (Harbor standard) |
| **MCP** | Model Context Protocol (open tool extension standard) |
| **ACP** | Agent Client Protocol (subscription-based provider auth) |
| **Recipe** | Parameterized YAML workflow (Goose pattern) |
| **Mono-tool** | Single tool surface (Terminus tmux-only design) |
| **Marker polling** | Early command completion detection via echo markers |
| **Rigorous mode** | Optional plan → execute → verify with critic |

---

*End of RFC v0.1*
