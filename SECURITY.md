# Security policy

## Supported versions

Security fixes target the latest published release and `main`.

## Report a vulnerability

Open the repository's [Security page](https://github.com/Darshan2104/Garuda-openagent/security)
and select **Report a vulnerability** if that option is available. GitHub sends
that report privately to the maintainers.

If the option is unavailable, open a
[public issue](https://github.com/Darshan2104/Garuda-openagent/issues/new) asking
the maintainers for a private reporting channel. Do not put vulnerability
details, reproductions, secrets, or tokens in that issue. Once you have a
private channel, include the Garuda version, operating system, workspace kind,
runtime, and steps to reproduce. This conditional workflow follows
[GitHub's private-reporting guidance](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately).

## Security scope

Permission rules, read-only and no-edits modes, and the `sandbox` workspace
are guardrails, not sandbox boundaries. macOS Seatbelt does not confine host
file reads. External ACP harnesses run with their own authority.

Use a Docker workspace for untrusted workspace commands. Garuda itself, model
calls, MCP servers, and host tools remain outside that container. External
harnesses require their own controls and an independently isolated workspace.

Read [Safety and workspaces](docs/guides/safety-and-workspaces.md) and
[External harnesses](docs/guides/external-harnesses.md) for the controls and
their limits.
