# Runtime session-reference resolution implementation plan

## Scope

Make all session-bound `garuda runtime` commands accept the same unique
prefixes and `latest` reference supported by `SessionStore.resolve`.

## Steps

1. In the runtime-command dispatcher, resolve `args.session` once before any
   preview, context-pack creation, lease acquisition, or runtime operation.
2. Convert missing or ambiguous references into an stderr diagnostic and exit
   code 2, preserving the existing no-mutation preview guarantee.
3. Pass the canonical full ID to handoff, resume, recover, reclaim, and
   support paths.
4. Add CLI tests for successful unique-prefix handoff preview and for
   ambiguous/missing prefix refusal without changed metadata.
5. Run focused runtime CLI tests, lint, docs contract checks, and the full
   suite; then review and merge through `Darshan2104` only.
