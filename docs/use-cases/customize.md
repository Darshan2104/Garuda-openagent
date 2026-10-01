# Level 3 · Teach it your project

<span class="gd-level">Level 3</span> Add files to your repository so every
run knows your conventions, uses the right tools, and follows your rules.

```text
your-project/
├── AGENTS.md              # standing instructions (or GARUDA.md)
└── .agent/                # .garuda/ also works
    ├── agents/            # your own profiles: <name>.yaml or <name>.md
    ├── skills/            # <name>/SKILL.md procedures
    ├── tools/             # Python tools (opt-in, runs repo code)
    ├── mcp.json           # MCP servers
    └── settings.yaml      # project defaults
```

!!! warning "Cloned repositories can ship these files too"
    Before running Garuda in someone else's repository, read its `AGENTS.md`,
    `.agent/agents/`, and `.agent/mcp.json`. A project profile can replace a
    built-in one, and MCP server commands run on your machine when Garuda
    connects. Python tools and hooks never run unless **you** opt in.

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
agent's instructions on every run in that workspace.

## Create your own agent profile

<p class="gd-facts">Applies to: runs with <code>--agent &lt;name&gt;</code></p>

**Use it when** a job needs its own tool set, rules, or persona.

Create `.agent/agents/docs-writer.yaml`:

```yaml
name: docs-writer
description: Edits Markdown documentation
permission_mode: smart
max_turns: 60
tools: [read_file, write_file, edit, grep, glob, ls, bash, task_complete]
path_rules:
  deny: [".env", "**/*.pem", "secrets/**"]   # never read or write these
  ask: ["src/**"]                            # needs approval
bash_rules:
  allow_prefixes: ["git diff", "git status"] # always allowed
  deny: ['\bgit\s+push\b']                   # regex; deny always wins
system_prompt: |
  You are the technical writer for this project.
  Edit only Markdown files under docs/ and README.md.
  Finish with task_complete and a short summary of what changed.
```

Then use it:

```bash
garuda run --agent docs-writer -t "Update the install section of README.md for Python 3.12"
```

**What happens**

- Garuda looks for the profile in `.agent/agents/`, then `.garuda/agents/`, then
  the built-ins. A project file named after a built-in (for example
  `build.yaml`) replaces it in that project.
- `tools` limits what the agent can call. `path_rules` and `bash_rules` are
  enforced on every call: deny beats allow, and in `garuda run` an "ask" is
  denied because nobody is there to approve it.
- `system_prompt` is guidance, not enforcement. Use rules for anything that
  must not happen.

??? note "Prefer Markdown? Use `agent.md` format"

    `.agent/agents/docs-writer.md` works too: put the settings in YAML front
    matter and write the system prompt as the body.

    ```markdown
    ---
    name: docs-writer
    description: Edits Markdown documentation
    permission_mode: smart
    tools: [read_file, write_file, edit, grep, glob, ls, task_complete]
    ---

    You are the technical writer for this project. Edit only Markdown files.
    ```

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
which keeps the prompt small. If the profile doesn't grant a tool listed in
`allowed-tools`, Garuda logs a warning.

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

Check it, then connect and list the tools:

```bash
garuda mcp list --no-connect
garuda mcp list
```

**What happens**

- `command` servers start as local processes over stdio. `url` servers are
  reached over HTTP.
- Project and global (`~/.agent/mcp.json`) servers are merged; the project wins
  on a name clash. Set `GARUDA_MCP_MERGE=0` to use only one file.
- With many tools, Garuda exposes `search_tool` and `use_tool` instead of every
  schema, to keep the prompt small.
- MCP tool calls are permission-screened like any other tool. Use
  `--mcp-config FILE` to pick a config for one run.

More in [Configuration → MCP](../guides/configuration.md#mcp).

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

**Next:** run Garuda from files, scripts, and programs in
[Level 4 · Automate](automate.md).
