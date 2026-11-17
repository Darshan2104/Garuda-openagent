"""Discover repository launch trees without importing or executing project code."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_PATH_FLAGS = {"-r", "--require", "--import", "--loader"}
_CODE_SUFFIXES = {".py", ".pyw", ".js", ".cjs", ".mjs", ".ts", ".sh", ".rb", ".pl"}
_MAX_PACKAGE_FILES = 4096
_CODE_ENV = {"NODE_OPTIONS", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "LD_PRELOAD",
             "LD_LIBRARY_PATH", "RUBYOPT", "PERL5OPT", "JAVA_TOOL_OPTIONS"}


class LaunchBindingError(ValueError):
    """Launch files cannot be safely enumerated."""


@dataclass
class LaunchFiles:
    paths: list[Path] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    trees: list[Path] = field(default_factory=list)
    unsafe_bytecode: list[Path] = field(default_factory=list)

    @property
    def unpinned(self) -> bool:
        return bool(self.reasons)

    def mark_unpinned(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)


def is_python_command(command: str) -> bool:
    return bool(re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?(?:\.exe)?", Path(command).name))


def python_launch_environment(
    workspace: str | None, command: str, args: list[str], environment: dict[str, str],
) -> dict[str, str]:
    """Ignore repository bytecode using an owner-only cache outside the project."""
    import hashlib

    from garuda.config.agent_home import global_settings_path

    if workspace is None:
        raise LaunchBindingError("project Python launch requires a workspace root")
    # Python's -E/-I ignore our environment; -X can override its cache prefix.
    # Only inspect interpreter options, never the server's own arguments.
    index = 0
    while index < len(args):
        option = args[index]
        if option == "--" or not option.startswith("-") or option.startswith(("-m", "-c")):
            break
        if option in {"-W", "--check-hash-based-pycs"}:
            index += 2
            continue
        if option == "-X":
            index += 1
            option = "-X" + (args[index] if index < len(args) else "")
        if option.startswith("-X"):
            unsafe = option[2:].split("=", 1)[0] == "pycache_prefix"
        else:
            unsafe = not option.startswith(("--", "-W")) and any(flag in option for flag in "EI")
        if unsafe:
            raise LaunchBindingError("Python interpreter option overrides project bytecode isolation")
        index += 1
    root = Path(workspace).resolve()
    files = discover_launch_files([command, *args], root, environment)
    if files.unsafe_bytecode:
        # PYTHONPYCACHEPREFIX redirects source caches, but Python still loads
        # legacy sourceless .pyc imports. Refuse instead of executing unbound bytes.
        raise LaunchBindingError("sourceless project bytecode is unsupported; provide Python source")
    home = global_settings_path().expanduser().parent.resolve()
    if home.is_relative_to(root):
        raise LaunchBindingError("Python cache home must be outside the project")
    cache = home
    identity = hashlib.sha256(str(root).encode()).hexdigest()
    if (home / "cache" / "mcp-python" / identity).is_relative_to(root):
        raise LaunchBindingError("Python cache directory must be outside the project")
    for name in ("cache", "mcp-python", identity):
        cache = cache / name
        if cache.is_symlink():
            raise LaunchBindingError("Python cache directory must not be a symlink")
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        if cache.stat().st_uid != os.getuid():
            raise LaunchBindingError("Python cache directory has a foreign owner")
        cache.chmod(0o700)
    return {**environment, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": str(cache)}


def _within(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _bytecode(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}


def discover_launch_files(
    tokens: list[str], workspace: Path, environment: dict[str, str] | None = None,
) -> LaunchFiles:
    """Bind local modules/packages and script-containing trees, excluding bytecode.

    No sys.path lookup or import is allowed. Every tree uses the same enumeration
    bound; failures refuse the grant instead of silently truncating its coverage.
    """
    found = LaunchFiles()
    walked: set[Path] = set()
    for variable in sorted(environment or {}):
        if variable in _CODE_ENV or variable.startswith("DYLD_"):
            found.mark_unpinned(f"code-loading environment variable {variable}")
    command = Path(tokens[0]).name if tokens else ""
    if command in {"npx", "uvx", "bunx", "pipx"} or (
        command in {"uv", "pnpm"} and ("run" if command == "uv" else "dlx") in tokens[1:]
    ):
        found.mark_unpinned("package runner executes installed/downloaded packages whose code is not pinned")

    def add_path(value: str, *, required: bool = False, bind_tree: bool = True) -> None:
        code_like = "/" in value or "\\" in value or Path(value).suffix in _CODE_SUFFIXES
        if not value:
            if required:
                found.mark_unpinned("missing code operand")
            return
        raw = Path(value).expanduser()
        candidate = Path(os.path.abspath(raw if raw.is_absolute() else workspace / raw))
        if not _within(candidate, workspace):
            if required or code_like:
                found.mark_unpinned(f"code/path operand outside project: {value}")
            return
        if candidate.is_file() or candidate.is_symlink():
            if _bytecode(candidate):
                found.unsafe_bytecode.append(candidate)
                found.mark_unpinned(f"direct bytecode operand is not pinned: {value}")
                return
            found.paths.append(candidate)
            if not (candidate.is_file() and _within(candidate.resolve(), workspace)):
                found.mark_unpinned(f"code/path operand leaves project: {value}")
            elif bind_tree:
                add_tree(candidate.parent)
        elif required or code_like:
            found.mark_unpinned(f"unresolved code/path operand: {value}")

    def add_tree(base: Path) -> None:
        if base in walked:
            return
        walked.add(base)
        found.trees.append(base)
        count = 0
        visited: set[Path] = set()
        for directory, dirs, files in os.walk(base, followlinks=True, onerror=_walk_error):
            path = Path(directory)
            resolved = path.resolve(strict=True)
            found.paths.append(path)
            count += 1
            if count > _MAX_PACKAGE_FILES:
                raise LaunchBindingError("launch tree exceeds the launch-file limit")
            if not _within(resolved, workspace):
                found.mark_unpinned(f"launch tree leaves project: {path.relative_to(workspace)}")
                dirs.clear()
                continue
            if resolved in visited:
                dirs.clear()
                continue
            visited.add(resolved)
            dirs[:] = sorted(name for name in dirs if not _bytecode(path / name))
            for name in sorted(files):
                if _bytecode(path / name):
                    if not (path / name).with_suffix(".py").is_file():
                        found.unsafe_bytecode.append(path / name)
                    continue
                count += 1
                if count > _MAX_PACKAGE_FILES:
                    raise LaunchBindingError("launch tree exceeds the launch-file limit")
                add_path(str(path / name), required=True, bind_tree=False)

    def add_module(value: str) -> None:
        parts = value.split(".")
        if any(not part or any(char in part for char in ("/", "\\", "\0")) for part in parts):
            found.mark_unpinned(f"unresolved Python module: {value}")
            return
        base = workspace
        package_root = None
        for index, part in enumerate(parts):
            package = base / part
            module = base / (part + ".py")
            regular_package = (package / "__init__.py").is_file()
            if not regular_package and not module.is_file() and module.with_suffix(".pyc").is_file():
                found.unsafe_bytecode.append(module.with_suffix(".pyc"))
                found.mark_unpinned(f"unresolved Python module: {value}")
                return
            if not regular_package and module.is_file():
                if index != len(parts) - 1:
                    found.mark_unpinned(f"unresolved Python module: {value}")
                    return
                add_path(str(module), required=True, bind_tree=False)
                add_tree(package_root if package_root is not None else module.parent)
                return
            if not package.is_dir() or not _within(package.resolve(), workspace):
                found.mark_unpinned(f"unresolved Python module: {value}")
                return
            if regular_package:
                add_path(str(package / "__init__.py"), required=True, bind_tree=False)
            else:
                found.mark_unpinned(f"namespace package may be shadowed by installed code: {value}")
            if package_root is None:
                package_root = package
            base = package
        add_tree(package_root)

    index = 0
    options = True
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if options and token == "--":
            options = False
        elif options and token in _PATH_FLAGS | {"-m"}:
            value = tokens[index] if index < len(tokens) else ""
            index += 1
            if token == "-m":
                add_module(value)
            else:
                add_path(value, required=True)
        elif options and token.startswith("-"):
            flag, separator, value = token.partition("=")
            if separator:
                add_path(value, required=flag in _PATH_FLAGS)
            elif token.startswith("-m") and not token.startswith("--"):
                add_module(token[2:])
            elif token.startswith("-r") and not token.startswith("--"):
                add_path(token[2:], required=True)
        else:
            add_path(token)
    return found


def _walk_error(error: OSError) -> None:
    raise LaunchBindingError("cannot enumerate launch tree") from error
