# Level 4 · Automate

<span class="gd-level">Level 4</span> Run Garuda from files, shell scripts, CI
jobs, Python programs, and HTTP clients.

| Approach | Best for | Starts with |
|---|---|---|
| Recipe | A fixed sequence of steps with parameters | `garuda recipe run file.yaml` |
| Shell or CI | One task with an exit status and JSON output | `garuda run --json` |
| Python SDK | Embedding the agent in your own program | `SoftwareAgent` |
| HTTP service | Submitting and polling jobs from another process | `garuda serve` |

## Run a multi-step recipe

<p class="gd-facts">Changes files: depends on the steps · Format: YAML</p>

**Use it when** you run the same plan → build → test sequence often.

Save this as `fix-and-test.yaml`:

```yaml
name: fix-and-test
description: Analyze an issue, implement a fix, and run tests
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
    prompt: "Run {{test_command}} and fix any failures related to: {{issue}}"
```

Run it:

```bash
garuda recipe run fix-and-test.yaml --param issue="Login fails when the email has uppercase letters" --param test_command="pytest -q"
```

**What happens**

- Steps run in order. Each step's output is appended to the next step's prompt
  under "Prior step output".
- `{{name}}` placeholders are filled from `--param KEY=VALUE`, then from
  defaults. A missing required parameter stops the recipe before any step runs.
- Each step can set `agent` (a profile) and `mode`.
- The recipe stops at the first failed step and tells you which steps did not
  run.

## Use Garuda in scripts and CI

<p class="gd-facts">Exit status: 0 when the run succeeds, non-zero otherwise</p>

**Use it when** another program decides what to do with the result.

=== "Shell script"

    ```bash
    if garuda run --mode eval --deadline-sec 1800 -t "Make the test suite pass"; then
      echo "Garuda accepted the result"
    else
      echo "Garuda could not finish" >&2
    fi
    ```

=== "JSON events"

    ```bash
    garuda run --json --mode readonly -t "List the public functions in src/api" > events.jsonl
    jq -r 'select(.type == "tool_call") | .payload.name' events.jsonl
    jq 'select(.type == "session_end") | .payload' events.jsonl
    ```

    `--json` prints one JSON object per line with `type`, `timestamp`,
    `session_id`, and `payload`. Types include `session_start`, `tool_call`,
    `tool_result`, `model_response`, `verification`, and `session_end`.

=== "GitHub Actions"

    ```yaml
    # .github/workflows/garuda-review.yml
    name: Garuda review
    on: pull_request
    jobs:
      review:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
            with:
              fetch-depth: 0
          - uses: actions/setup-python@v5
            with:
              python-version: "3.12"
          - run: pip install git+https://github.com/Darshan2104/Garuda-openagent.git
          - run: git diff "origin/${{ github.base_ref }}...HEAD" > review.diff
          - run: garuda run --agent reviewer --mode readonly -t "Review the changes in review.diff and list concrete problems"
            env:
              OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
    ```

    Read-only mode is a guardrail, and the pull request's content reaches the
    model. Run this only for branches whose content you are willing to send to
    your provider.

Add `--trajectory run.jsonl` to also save the events to a file after the run.

## Call Garuda from Python

<p class="gd-facts">Async API · Runs the same native loop as the CLI</p>

**Use it when** Garuda is one part of a larger Python program.

=== "One task"

    ```python
    import asyncio

    from garuda.sdk import SoftwareAgent


    async def main():
        agent = SoftwareAgent(workspace=".", agent="build", mode="readonly")
        result = await agent.run("Summarize this repository")
        print("success:", result.success, "turns:", result.turns)
        print(result.final_message)


    asyncio.run(main())
    ```

=== "Conversation"

    ```python
    import asyncio

    from garuda.sdk import SoftwareAgent


    async def main():
        conversation = SoftwareAgent(workspace=".").conversation()
        try:
            first = await conversation.run("Find the slowest test in tests/")
            print(first.final_message)
            second = await conversation.run("Now make it faster without changing what it checks")
            print(second.final_message)
        finally:
            await conversation.close()


    asyncio.run(main())
    ```

=== "Your own tool"

    ```python
    import asyncio

    from garuda.sdk import SoftwareAgent
    from word_count import WordCountTool  # the class from Level 3


    async def main():
        agent = SoftwareAgent(workspace=".")
        agent.register_tool(WordCountTool())
        result = await agent.run("How many words are in README.md?")
        print(result.final_message)


    asyncio.run(main())
    ```

`SoftwareAgent` also accepts `model=`, `workspace_kind=` (for example
`"docker"`), `docker_image=`, and `runtime=` to run one turn on an
[external harness](advanced.md#run-a-task-with-claude-code-codex-or-another-harness).
`agent.run(task, resume="latest")` continues a saved session.

## Call Garuda over HTTP

<p class="gd-facts">JSON-RPC 2.0 over HTTP POST · Loopback by default · Bearer token required</p>

**Use it when** an editor plugin, service, or another language needs to submit
tasks.

Start the server with a token you choose:

```bash
export GARUDA_SERVE_TOKEN=replace-with-a-long-random-string
garuda serve --workspace . --port 8765
```

If you don't set a token on loopback, Garuda generates one and prints it once.
Binding a non-loopback `--host` without a token is refused.

Submit a job, then poll it:

```bash
curl -s http://127.0.0.1:8765/ -H "Authorization: Bearer $GARUDA_SERVE_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"submit","params":{"task":"Summarize this repository","mode":"readonly"}}'

curl -s http://127.0.0.1:8765/ -H "Authorization: Bearer $GARUDA_SERVE_TOKEN" \
  -d '{"jsonrpc":"2.0","id":2,"method":"status","params":{"job_id":"JOB_ID"}}'

curl -s http://127.0.0.1:8765/ -H "Authorization: Bearer $GARUDA_SERVE_TOKEN" \
  -d '{"jsonrpc":"2.0","id":3,"method":"result","params":{"job_id":"JOB_ID"}}'
```

Replace `JOB_ID` with the `job_id` returned by `submit`.

| Method | Does |
|---|---|
| `health` | Returns `{"status": "ok", "version": …}` |
| `submit` | Starts a job. Params: `task`, plus optional `agent`, `mode`, `workspace`, `workspace_kind`, `model`, `resume` |
| `status` | Job state, turn count, and event count |
| `events` | New events since `cursor`, for streaming progress |
| `result` | `success`, `final_message`, and `turns` once the job is done |
| `cancel` | Stops a job |
| `run` | Runs a task and waits; returns the result and all events |
| `jobs`, `sessions`, `list_agents` | Lists jobs, saved sessions, and profiles |
| `runtime_list`, `runtime_inspect`, `runtime_handoff`, `runtime_recover`, `runtime_support` | Runtime operations; see [Level 5](advanced.md) |

Requests that carry a browser `Origin` header are rejected, so a web page you
visit can't drive the server. `--max-jobs` limits concurrent jobs (default 4).

---

**Next:** use external harnesses, handoffs, and routing in
[Level 5 · Advanced](advanced.md).
