"""Auth UX tests for issue #35 (P1.5).

Absent, logged-out, unknown-quota, and delegated-login states: login opens
only the manifest's user-driven flow, quota surfaces only when supplied, and
no code path reads credential stores.
"""

import ast
import pathlib
import re
import sys

from garuda.acp.adapter import AcpRuntime
from garuda.acp.catalog import builtin_manifest_dicts, discover, health_of
from garuda.runtime.registry import LoginFlow, parse_global_manifests

FAKE = [sys.executable, "-m", "garuda.acp.fake_agent"]


def test_login_flows_are_user_driven_only():
    for manifest in parse_global_manifests(builtin_manifest_dicts()):
        assert manifest.login.flow in ("user-cli", "api-key")
        assert manifest.login.instructions, manifest.runtime_id
    assert LoginFlow.parse(None) == LoginFlow()
    assert LoginFlow.parse({"flow": "api-key", "instructions": "export it"}).flow == "api-key"
    for bad in (
        {"flow": "browser-sso"},
        {"flow": "user-cli", "instructions": ["x"]},
        {"flow": "user-cli", "bogus": 1},
        ["not-a-mapping"],
    ):
        try:
            LoginFlow.parse(bad, where="t.login")
        except Exception:
            continue
        raise AssertionError(f"login accepted {bad!r}")


def test_absent_and_logged_out_states_guide_without_scanning():
    manifests = parse_global_manifests(builtin_manifest_dicts())
    found = {d.runtime_id: d for d in discover(manifests)}
    for vendor in ("claude", "codex", "cursor", "opencode", "pi", "goose"):
        auth_lines = found[vendor].describe_auth()
        assert any("unknown" in line or "logged out" in line for line in auth_lines)
        assert any("to log in:" in line for line in auth_lines)
        assert any("vendor's policy" in line for line in auth_lines)
        health = health_of(found[vendor])
        assert health["quota"] is None
        assert health["login"]["flow"] == "user-cli"


#: Path components that name a vendor credential store. Matched exactly
#: against each `/`- or whitespace-separated piece of a string constant, so
#: `Path.home() / ".codex" / "auth.json"` is caught piece by piece.
CREDENTIAL_COMPONENTS = frozenset(
    {".codex", "auth.json", "credentials.json", ".credentials.json"}
)
#: Substrings no production string constant may contain.
CREDENTIAL_SUBSTRINGS = (
    "auth.json",
    "credentials.json",
    "Claude Code-credentials",
    "Keychain",
    "keychain",
    "oauth",
    "OAuth",
    "find-generic-password",
    "find-internet-password",
    "dump-keychain",
)
#: Secret-store libraries: importing any of them anywhere in garuda/ fails.
FORBIDDEN_MODULES = frozenset({"keyring", "secretstorage", "win32cred", "keychain"})
#: Identifiers (function names, attributes, names) that read or move tokens.
FORBIDDEN_IDENTIFIER = re.compile(
    r"^(get|read|load|copy|proxy|fetch)_(oauth_)?(token|tokens|credential|credentials|password)$"
    r"|^(token|credential|credentials)_path$"
    r"|oauth",
    re.IGNORECASE,
)
#: Secret-store API names, flagged however the module was imported.
FORBIDDEN_CALLS = frozenset({"get_password", "get_credential", "find_generic_password"})


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Docstrings are the allowlisted setup/guidance text: prose that says
    what Garuda never touches, never a value the code uses."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    ids.add(id(body[0].value))
    return ids


def _folded(node: ast.AST) -> str | None:
    """Constant-fold `"a" + "b"` chains so split literals cannot hide a path."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded(node.left), _folded(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _string_hits(text: str) -> list[str]:
    hits = [marker for marker in CREDENTIAL_SUBSTRINGS if marker in text]
    if text in FORBIDDEN_MODULES:  # importlib.import_module("keyring")
        hits.append(f"module name {text}")
    components = set(re.split(r"[\\/\s'\"]+", text))
    hits.extend(sorted(components & CREDENTIAL_COMPONENTS))
    return hits


def _scan_source(source: str, where: str = "<source>") -> list[str]:
    """Token-level credential scan of one module: string constants (and folded
    `+` chains) outside docstrings, imports of secret-store libraries, and
    token-helper identifiers."""
    tree = ast.parse(source, filename=where)
    docstrings = _docstring_nodes(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        text = _folded(node)
        if text is not None:
            hits.extend(f"{where}:{node.lineno}:{marker}" for marker in _string_hits(text))
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            names = []
        for name in names:
            if name.split(".")[0] in FORBIDDEN_MODULES:
                hits.append(f"{where}:{node.lineno}:import {name}")
        identifiers: list[str] = []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            identifiers.append(node.name)
        elif isinstance(node, ast.Name):
            identifiers.append(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.append(node.attr)
        elif isinstance(node, ast.arg):
            identifiers.append(node.arg)
        for identifier in identifiers:
            if FORBIDDEN_IDENTIFIER.search(identifier) or identifier in FORBIDDEN_CALLS:
                hits.append(f"{where}:{getattr(node, 'lineno', 0)}:{identifier}")
    return sorted(set(hits))


def test_credential_scanner_catches_planted_readers():
    """The gate itself is tested: each known bypass shape must be flagged, and
    guidance prose in a docstring must not be."""
    planted = {
        "home-join": 'from pathlib import Path\nP = Path.home() / ".codex" / "auth.json"\n',
        "os-join": 'import os\nP = os.path.join(os.environ["HOME"], ".claude", ".credentials.json")\n',
        "split-literal": 'P = "~/.codex/auth" + ".json"\n',
        "keyring": 'import keyring\nT = keyring.get_password("Claude Code-credentials", "me")\n',
        "keyring-from": 'from keyring import get_password\n',
        "keyring-dynamic": 'import importlib\nK = importlib.import_module("keyring")\n',
        "security-cli": 'ARGV = ["security", "find-generic-password", "-s", "x"]\n',
        "helper": "def read_token():\n    return None\n",
        "oauth-helper": "def refresh_oauth():\n    return None\n",
    }
    for label, source in planted.items():
        assert _scan_source(source, label), f"scanner missed {label}"
    guidance = 'def f():\n    """Garuda never reads ~/.codex/auth.json or the Keychain."""\n'
    assert _scan_source(guidance) == []
    assert _scan_source("cache_read_tokens = 0\nthinking_budget_tokens = 1\n") == []


def test_no_python_reads_credential_stores():
    """Every module in the `garuda/` package — interfaces, agents, config, and
    the rest, not only acp/ and runtime/ — passes the token-level scan."""
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    package = repo_root / "garuda"
    scanned = sorted(p for p in package.rglob("*.py") if p.is_file())
    # Set-checked: prove the scan reached production files across packages.
    rel = {p.relative_to(package).as_posix() for p in scanned}
    for required in (
        "acp/catalog.py",
        "acp/adapter.py",
        "acp/client.py",
        "runtime/registry.py",
        "agents/setup.py",
        "config/agent_home.py",
        "interfaces/main.py",
        "sdk/software_agent.py",
    ):
        assert required in rel, f"credential scan missed {required!r}"
    assert len(scanned) >= 100, f"credential scan covered too few files ({len(scanned)})"
    hits: list[str] = []
    for path in scanned:
        hits.extend(
            _scan_source(path.read_text(encoding="utf-8"), path.relative_to(repo_root).as_posix())
        )
    assert hits == [], f"credential-store references in code: {hits}"


async def test_quota_passes_through_only_when_supplied():
    plain = AcpRuntime([*FAKE, "--profile", "success"], runtime_id="q-plain")
    await plain.start(task="t", session_id="q1")
    assert plain.quota is None
    await plain.close()

    quota = '{"remaining": 42, "unit": "credits"}'
    rich = AcpRuntime(
        [*FAKE, "--profile", "success", "--quota-json", quota], runtime_id="q-rich"
    )
    await rich.start(task="t", session_id="q2")
    assert rich.quota == {"remaining": 42, "unit": "credits"}
    await rich.close()


def test_project_disable_suggestion_keeps_login_guidance():
    manifests = parse_global_manifests(builtin_manifest_dicts())
    plain = {d.runtime_id: d for d in discover(manifests, run_probe=lambda *a, **k: None)}
    warned = {
        d.runtime_id: d
        for d in discover(manifests, project_disabled={"codex"}, run_probe=lambda *a, **k: None)
    }
    assert any("ignored" in w for w in warned["codex"].warnings)
    assert warned["codex"].login_flow == plain["codex"].login_flow
    assert warned["codex"].login_instructions == plain["codex"].login_instructions
    assert warned["codex"].login_instructions

