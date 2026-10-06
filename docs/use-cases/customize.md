# Level 3 · Teach it your project

<span class="gd-level">Level 3</span> Add files to your repository so every
run knows your conventions, uses the right tools, and follows your rules.

```text
your-project/
├── AGENTS.md              # standing instructions (or GARUDA.md)
├── garuda.yaml            # checks and flows for this project (see Level 5)
└── .agent/                # .garuda/ also works
    ├── agents/            # your own agents: <name>.yaml or <name>.md
    ├── skills/            # <name>/SKILL.md procedures
    ├── tools/             # Python tools (opt-in, runs repo code)
    ├── memory.md          # notes you accepted with `garuda memory review`
    ├── mcp.json           # MCP servers (start only after `garuda mcp trust`)
    └── settings.yaml      # project defaults
```

!!! warning "Cloned repositories can ship these files too"
    Before running Garuda in someone else's repository, read its `AGENTS.md`,
    `.agent/agents/`, `.agent/mcp.json`, `.agent/settings.yaml`, and
    `garuda.yaml`. A project agent can replace a built-in one, and project
    settings can turn on the collection model. The project's MCP servers and
    `garuda.yaml` checks don't run until you trust them, and Python tools and
    hooks never run unless **you** opt in.

## Give Garuda project instructions

<p class="gd-facts">Applies to: every run in this project</p>

**Use it when** you keep repeating the same guidance ("run tests with
`make test`", "never touch `migrations/`").

Create `AGENTS.md` (or `GARUDA.md`) at the project root:

```markdown
# Notes for coding agents

- Run the tests with `make test`. Never run `make deploy`.
- The API is in `src/api/`; the background worker is in `src/worker/`.
- Keep functions under 50 lines and add a test for every bug fix.
```

**What happens:** Garuda adds the first 8,000 characters of the file to the
agent's instructions on every run in that workspace. A longer file is cut at
that point, and Garuda says so: the prompt ends the section with a note, a
`memory.truncated` warning is printed, and the `session_start` event lists the
diagnostic.

## Create your own agent

<p class="gd-facts">Applies to: runs with <code>--agent &lt;name&gt;</code> · Format: version 1 agent definition</p>

**Use it when** a job needs its own instructions, tools, rules, or limits.

Start from a packaged agent and change only what you need:

```bash
garuda agent new docs-writer --from garuda/build --project
```

That writes `.agent/agents/docs-writer.yaml` (drop `--project` to write it to
`~/.agent/agents/` for all your projects). Edit it:

```yaml
version: 1
extends: garuda/build
description: Edits Markdown documentation
instructions:
  text: |
    You are the technical writer for this project.
    Edit only Markdown files under docs/ and README.md.
limits:
  max_turns: 60
tools:
  preset: none
  add: [read_file, write_file, edit, grep, glob, ls, bash, task_complete]
permissions:
  mode: smart
  rules:
    paths:
      deny: [".env", "**/*.pem", "secrets/**"]   # never read or write these
      ask: ["src/**"]                            # needs approval
    bash:
      allow_prefixes: ["git diff", "git status"] # skip the ask step
      deny: ['\bgit\s+push\b']                   # regex; deny always wins
```

Check it, then use it:

```bash
garuda agent check docs-writer
garuda run --agent docs-writer -t "Update the install section of README.md for Python 3.12"
```

```text
· agent.ok: project/docs-writer resolves (garuda/build -> project/docs-writer)
    fix: Nothing to do.
```

**What happens**

- `extends` takes everything from the parent and applies your changes:
  settings merge key by key, lists replace, and your `instructions` are added
  after the parent's (`mode: replace` drops the parent's text instead).
- `tools.preset` is `all`, `read-only` or `none`; `add` and `remove` edit the
  result. MCP tools and tools you register are added on top.
- In `smart` mode the path and bash rules are checked on every call: deny beats
  allow, and in `garuda run` an "ask" is denied because nobody is there to
  approve it.
- Version 1 is strict. A misspelled field, an unknown tool or a duplicate key
  refuses the agent with a code and a fix, before anything runs:

    ```text
    ✗ agent.unknown_field: limits.max_turn: is not a version 1 field
        fix: Remove or rename limits.max_turn; `garuda agent show` lists the fields.
    ```

- A name is looked up in the project (`.agent/agents/`, then
  `.garuda/agents/`), then in `~/.agent/agents/`, then among the packaged
  agents. `garuda/build` always means the packaged one.
- An agent inside the project can't raise its own permission mode above
  `smart` (`agent.project_widening`). Raise `agents.project_ceiling` in
  `~/.agent/settings.yaml`, or pass `--permission-mode` for one run.
- Instructions are guidance, not enforcement. Use rules for anything that must
  not happen.

Every field is listed in the [Agent definitions guide](../guides/agents.md).

??? note "Have an older profile without `version: 1`? Migrate it"

    Older profiles (`permission_mode:`, `path_rules:`, `system_prompt:` at the
    top level, or an `agent.md` with front matter) keep working. To move one to
    version 1:

    ```bash
    garuda agent migrate .agent/agents/docs-writer.yaml           # preview
    garuda agent migrate .agent/agents/docs-writer.yaml --write   # replace it, keeping a backup
    ```

    ```text
    [garuda] it resolves to exactly the same agent
    [garuda] preview only; --write replaces the file and keeps a backup
    ```

    If the version 1 form would behave differently, `--write` refuses
    (`agent.migrate_changes_behaviour`). Where it is only stricter, `--write`
    stops until you review it and pass `--accept-tightening`.

## See exactly what an agent will do

<p class="gd-facts">Changes files: no · Starts: no model, MCP server or hook</p>

**Use it when** you want to know which settings an agent really has, and where
each one came from, before you run it.

```bash
garuda agent list
garuda agent show docs-writer
garuda agent prompt docs-writer
```

```text
project/docs-writer          project   extends garuda/build Edits Markdown documentation
garuda/build                 packaged  Full-access agent for implementation work
garuda/explore               packaged  Fast read-only codebase exploration
...
```

- `agent list` shows every agent, where it comes from, what it extends, and
  any agent a nearer file shadows.
- `agent show` prints every effective field with its source: `packaged`,
  `user`, `project`, `extends:<agent>` or `default`.
- `agent prompt` prints the system prompt section by section, with its size,
  an estimated token count, and a digest. Each run records the digest of the
  prompt it actually sent.
- Secrets are redacted unless you add `--raw`. Add `--json` for scripts.

## Get structured JSON back

<p class="gd-facts">Native runs · Checked against: a JSON Schema</p>

**Use it when** a program, not a person, reads the result.

Give the agent an output schema. In `.agent/agents/api-summary.yaml`:

```yaml
version: 1
extends: garuda/explore
description: List the public functions of a package as JSON
instructions:
  text: Read the source files and report each public function.
output:
  schema: schemas/api-summary.json
```

And `.agent/agents/schemas/api-summary.json`:

```json
{
  "type": "object",
  "required": ["functions"],
  "additionalProperties": false,
  "properties": {
    "functions": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name", "summary"],
        "additionalProperties": false,
        "properties": {
          "name": {"type": "string"},
          "summary": {"type": "string"}
        }
      }
    }
  }
}
```

Read the result from Python:

```python
import asyncio

from garuda import SoftwareAgent


async def main():
    result = await SoftwareAgent(workspace=".", agent="api-summary").run(
        "List the public functions in src/calc"
    )
    print(result.output)


asyncio.run(main())
```

```text
{'functions': [{'name': 'add', 'summary': 'add(a, b) returns the sum of a and b.'}, ...]}
```

**What happens**

- The agent must finish with a `result` that matches the schema. A wrong
  shape is sent back with the reasons, for at most two repair turns; then
  the run fails with `agent.output_invalid` and returns no output.
- The schema is checked when the agent loads. Unsupported keywords (such as
  `pattern` or `format`) and references outside the file are refused, not
  ignored.
- `garuda run --agent api-summary` works too: the validated value is in the
  session's `task_complete` event, and the terminal shows the summary.
- A valid shape isn't proof the content is right. Add
  [checks](change-code.md#verify-the-result-with-your-own-check) for that.

## Let the agent propose notes to remember

<p class="gd-facts">Changes memory: only after you accept · Needs: a terminal to review</p>

**Use it when** the agent keeps rediscovering the same facts about your
project.

Turn on note proposals in an agent:

```yaml
memory:
  notes: propose
```

The agent gets a `remember` tool. Its proposals wait for you:

```bash
garuda memory list
garuda memory review
```

```text
dc6c49fc  scope: project  session: 563025e0-…  state: pending
  This project's tests run with 'python -m pytest -q'.
[a]ccept  [e]dit  [r]eject  [s]kip  [q]uit: a
  added to .agent/memory.md
```

**What happens**

- A proposal changes nothing until you accept it at a terminal. A headless run
  can propose but never accept.
- Accepted project notes go to `.agent/memory.md`; user notes go to
  `~/.agent/memory.md`. Both load into later runs as reviewed information, not
  as instructions.
- Proposals are short (500 characters, ten per task), and anything that looks
  like a secret is refused.

## Add a skill

<p class="gd-facts">Applies to: every run in this project · Format: compatible with Anthropic and OpenCode skills</p>

**Use it when** you have a repeatable procedure the agent should follow on
request, such as writing release notes.

Create `.agent/skills/release-notes/SKILL.md`:

```markdown
---
name: release-notes
description: Write release notes from the commits since the last tag
allowed-tools: bash, read_file, write_file
---

1. Find the last tag with `git describe --tags --abbrev=0`.
2. List the commits since it with `git log <tag>..HEAD --oneline`.
3. Group them under Added, Changed, and Fixed.
4. Write the result to RELEASE_NOTES.md.
```

```bash
garuda run -t "Write the release notes for the next version"
```

**What happens:** Garuda adds a one-line index entry per skill to the agent's
instructions. The agent opens the full `SKILL.md` only when the task needs it,
which keeps the prompt small. `allowed-tools` is advice to the model, not
enforcement; `garuda agent check` warns when an agent lacks a tool a skill
lists.

**Choose which skills an agent sees.** Skills come from this project
(`.agent/skills/`), then `~/.agent/skills/`, then the packaged ones; when two
share a name, the nearer one wins. An agent can narrow the set:

```yaml
skills:
  from: [project]            # ignore user and packaged skills
  include: [release-notes]   # only these (omit for all; [] for none)
  load: index                # names only, read on demand; `full` puts every body in the prompt
```

## Connect MCP tools

<p class="gd-facts">Applies to: every run in this project · Needs: an MCP server</p>

**Use it when** the agent needs tools from another system, such as an issue
tracker, documentation search, or a database.

Create `.agent/mcp.json`:

```json
{
  "mcpServers": {
    "docs-files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "./docs"]
    },
    "issues": {
      "url": "https://mcp.example.com/mcp"
    }
  }
}
```

Check it, trust its servers, then connect and list the tools:

```bash
garuda mcp list --no-connect
garuda mcp trust
garuda mcp list
```

**What happens**

- `command` servers start as local processes over stdio. `url` servers are
  reached over HTTP.
- A server defined in the project's own config doesn't start, and its URL isn't
  contacted, until you trust it. `garuda mcp trust` shows what each server
  would run and records your answer for that exact entry in this repository;
  editing the entry or a script it runs means trusting it again. Until then,
  runs skip it with `agent.untrusted_project_code`.
- Project and global (`~/.agent/mcp.json`) servers are merged; a trusted
  project entry wins on a name clash. Set `GARUDA_MCP_MERGE=0` to use only one file.
- With many tools, Garuda exposes `search_tool` and `use_tool` instead of every
  schema, to keep the prompt small.
- MCP tool calls are permission-screened like any other tool. Use
  `--mcp-config FILE` to pick a config for one run.

More in [Configuration → MCP](../guides/configuration.md#mcp).

## Block or log tool calls with a hook

<p class="gd-facts">Configured in: <code>~/.agent/settings.yaml</code> · Runs: your shell command before or after a tool</p>

**Use it when** a rule is easier to write as a script than as a permission
pattern, or you want your own log of every tool call.

Save `~/.agent/hooks/no-rm.sh` and make it executable (`chmod +x`):

```sh
#!/bin/sh
# Block any bash call that runs rm. Exit 2 blocks; exit 0 allows.
if python3 -c 'import json,sys; sys.exit(0 if "rm " in json.load(sys.stdin)["arguments"].get("command", "") else 1)'; then
  echo "rm is not allowed here" >&2
  exit 2
fi
exit 0
```

Register it:

```yaml
# ~/.agent/settings.yaml
hooks:
  before_tool:
    - match: "bash"
      command: "~/.agent/hooks/no-rm.sh"
      timeout: 10
```

```text
Hook command '~/.agent/hooks/no-rm.sh' blocked tool bash (exit code 2)
```

**What happens**

- A hook receives the event as JSON on stdin: `event`, `tool` and
  `arguments` (and the result, for `after_tool`).
- A `before_tool` hook is a guard and fails closed: exit 0 allows the call,
  exit 2 blocks it, and anything else (another exit code, a crash, a timeout)
  blocks it too. Add `on_failure: allow` to make one of your own hooks
  advisory.
- Hooks in a project's `.agent/settings.yaml` run only if you set
  `trust_project_hooks: true` in your global settings, and they always fail
  closed.

More in [Configuration → permissions and hooks](../guides/configuration.md#permissions-and-hooks).

## Add your own Python tools

<p class="gd-facts">Applies to: runs where you opt in · Runs: repository code on your machine</p>

**Use it when** no built-in or MCP tool does what you need.

Create `.agent/tools/word_count.py`:

```python
from garuda.tools.protocol import ToolEffect
from garuda.types import ToolResult


class WordCountTool:
    name = "word_count"
    description = "Count the words in a text file in the workspace."
    effect = ToolEffect.READ_ONLY
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File to count"}},
        "required": ["path"],
    }

    async def execute(self, arguments, env, ctx):
        text = await env.read_file(arguments["path"])
        return ToolResult(tool_call_id="", content=f"{len(text.split())} words")


TOOLS = [WordCountTool]
```

Opt in for one run:

```bash
garuda run --load-project-tools -t "How many words are in README.md?"
```

**What happens**

- Garuda imports every `.agent/tools/*.py` module and loads the tools listed in
  `TOOLS` (or returned by `get_tools()`).
- Importing a module **runs its code**, so a repository can never turn this on
  by itself. Opt in per run with `--load-project-tools`, or for all projects
  with `load_project_tools: true` in `~/.agent/settings.yaml`.
- Project hooks follow the same rule, with `trust_project_hooks: true`. See
  [Safety → trusted configuration](../guides/safety-and-workspaces.md#trusted-configuration-and-executable-extensions).

---

**Next:** run several tasks at once in
[Level 4 · Run work in parallel](parallel.md).
