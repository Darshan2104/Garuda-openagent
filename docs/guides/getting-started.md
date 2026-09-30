# Getting started

This guide takes you from a clean checkout to a safe, read-only Garuda session.
It uses the native Garuda runtime and the built-in OpenRouter model default.

## Before you install

You need:

- Python 3.12 or newer and Git;
- a workspace you are comfortable letting a model inspect; and
- an API key for the provider behind your selected model.

Model-backed runs can cost money. Garuda's built-in default is
`openrouter/deepseek/deepseek-v4-flash-0731`, so the example below uses an
OpenRouter key. Provider prompts can include task text and tool results from the
workspace. Do not start in a directory containing secrets you do not intend to
share with that provider.

## Install Garuda

Clone the repository and install it in a virtual environment:

```bash
git clone https://github.com/Darshan2104/Garuda-openagent.git
cd Garuda-openagent
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
garuda --help
```

On Windows, activate the environment with the appropriate script under
`.venv\Scripts` instead of `source`.

Optional extras are `.[dev]` for contributors, `.[docs]` for PDF and
spreadsheet tools, `.[eval]` for Harbor, and `.[observability]` for
OpenTelemetry export.

## Configure the model

Export the credential for the default model:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

The value shown is a placeholder, not a real key. To use another
LiteLLM-compatible model, set `GARUDA_MODEL` to its `provider/model` identifier
and export the matching provider credential. See
[Configuration](configuration.md#models) for model bindings and the optional
collection role.

## Run a safe first task

From the workspace you want to inspect, run:

```bash
garuda run --workspace . --mode readonly -t "Summarize this repository and identify its main entry points"
```

`--mode readonly` forces read-only permissions over the selected profile and
screens shell commands for inspection-only use. It is a guardrail, not a
confinement boundary. Use a Docker workspace for untrusted code; the
`sandbox` workspace kind and macOS Seatbelt do not provide general host-read
confinement.

The normal command output identifies the selected reasoning model and ends with
the agent's final response. Exact wording depends on the model and repository,
so this guide does not prescribe a sample answer. The command exits nonzero if
the run fails.

## Find the saved session

Once execution starts, Garuda creates a durable session. List recent sessions
after the first run:

```bash
garuda sessions
```

The table shows an ID prefix, status, turn count, update time, and task. By
default, complete session directories live under `~/.agent/sessions/<id>/` and
contain metadata, messages, and an append-only event log. Set
`GARUDA_SESSIONS_DIR` to choose another root.

Resume the newest native session with a follow-up task:

```bash
garuda run --workspace . --mode readonly --resume latest -t "Explain the test layout"
```

Session-taking commands also accept a full ID or an unambiguous prefix from
`garuda sessions`.

## Choose the next interface

- Continue with one-shot tasks using `garuda run`.
- Start a conversation with permission prompts using
  `garuda chat --workspace . --mode readonly`.
- Browse saved runs locally with `garuda web --read-only`.
- Read [Using Garuda](using-garuda.md) before enabling mutations, changing
  workspaces, loading project tools, or selecting an external runtime.

An ACP runtime is not equivalent to the native loop. Garuda records the ACP
session lifecycle and workspace delta, but it does not apply the native
completion verifier to the ACP result. See
[External harnesses](external-harnesses.md) before using `--runtime` or
handoff commands.

## Common setup problems

**`garuda: command not found`**

Activate the virtual environment again, or confirm the environment's `bin`
directory is on `PATH`. Run `python -m pip show garuda-openagent` from the same
environment to confirm installation.

**Missing credential or provider authentication error**

Confirm the selected model and its provider credential. The default needs
`OPENROUTER_API_KEY`; another provider needs its own variable. Garuda does not
turn a vendor CLI login into an API key for the native runtime.

**Rate limit, quota, or unexpected cost**

Check the provider account named by the selected model. Start with a small task,
leave collection disabled unless needed, and use `--max-turns` or
`--deadline-sec` to bound a run. `eval` and `rigorous` modes use additional
model calls for completion gates.

**Workspace or sandbox refusal**

Garuda fails closed when a requested workspace backend is unavailable or a
workspace already has a live mutating lease. Do not bypass that refusal for
untrusted work. See the [CLI reference](../reference/cli.md#common-run-flags)
for backend flags and [Using Garuda](using-garuda.md#workspaces) for the safety
model.
