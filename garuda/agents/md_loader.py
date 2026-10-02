"""Load OpenCode-style agent.md profiles with YAML frontmatter."""

from pathlib import Path

from garuda.agents.loader import AgentProfile


def load_agent_md(path: str | Path) -> AgentProfile:
    """Parse one agent.md file through the resolver (H.1).

    Front matter holds the fields; the Markdown body is the instruction text.
    The file resolves on its own: an ``extends`` in it resolves against the
    packaged and user agents only.
    """
    import hashlib

    from garuda.agents import resolve

    target = Path(path)
    data = resolve._read(target, target.parent)
    source = resolve.Source(resolve.INLINE, target, hashlib.sha256(data).hexdigest(),
                            f"project/{target.stem}", target.parent)
    return resolve.activate(resolve.resolve_source(source, data, []))
