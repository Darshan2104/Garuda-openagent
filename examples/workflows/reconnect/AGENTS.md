# Reconnect example

This small retry client deliberately exposes only `connected`. Add observable
`disconnected`, `reconnecting`, and `connected` status without replacing its
transport or changing retry attempts and delays. Define failure behavior in the
plan and add deterministic status-transition tests. Existing tests protect the
retry contract; passing them alone does not prove the new status feature.

Use injected callbacks, never live sockets or sleeps. Run `python -m pytest`.
The example includes no agent answers or model configuration. Configure roles
through Garuda's existing setup, then follow `docs/use-cases/workflows.md` in the
Garuda documentation.
