# Runtime CLI session-reference resolution

**Date:** 2026-09-29

## Problem

`garuda sessions` renders shortened session IDs, but `garuda runtime handoff`
preview passes its `--session` value directly to the session store. A copied,
unique displayed prefix therefore raises a filesystem traceback even though the
store already defines prefix resolution for `resume`.

## Decision

Resolve the supplied session reference once in the runtime-command dispatcher
before invoking any session-bound operation. Apply the resolved full ID to
handoff preview and confirmation, recover, reclaim, and support. The existing
store resolver remains the sole authority for `latest`, unique prefixes,
ambiguous prefixes, and invalid values.

## Failure behavior

An unknown or ambiguous session reference is a user input error: print a
concise error to stderr, return exit code 2, and do not create a context pack,
lease, handoff record, or other state. Resolution happens before the preview
path too, so a preview cannot leak an unhandled traceback.

## Verification

Use real CLI dispatch tests to prove that a unique displayed-style prefix works
for preview, that confirmation receives the resolved ID, and that ambiguous or
missing prefixes produce the typed refusal without changing session metadata.
Run the runtime CLI test module, lint, documentation checks, and the full
suite before merge.

## Scope

This changes only CLI reference handling. It does not alter the session schema,
the handoff transaction, prefix matching rules, or provider/model behavior.
