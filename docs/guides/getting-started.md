# Getting started

## Install

```bash
git clone https://github.com/Darshan2104/Garuda-openagent.git
cd Garuda-openagent
pip install -e ".[dev]"
```

Optional extras are `.[docs]` for PDF and spreadsheet tools, `.[eval]` for Harbor, and `.[observability]` for OpenTelemetry exports.

## Configure a model

The default model is `openrouter/deepseek/deepseek-v4-flash-0731`. Give its provider an API key, or select another LiteLLM-compatible model and matching credential:

```bash
export OPENROUTER_API_KEY=sk-or-...
# or:
export ANTHROPIC_API_KEY=...
export GARUDA_MODEL=anthropic/claude-sonnet-5
```

## Run a first task

```bash
garuda run -t "List all Python files in the current directory"
garuda chat --agent build
garuda sessions
```

Use `--mode eval` for benchmark-grade completion gates and `--mode readonly` to inspect a workspace without writes. See [Using Garuda](using-garuda.md) before using permissive modes or host workspaces with untrusted code.
