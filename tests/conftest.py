import pytest


class _FakeModeTables(dict):
    """Garuda's mode tables, plus the fake agent's modes for any other runtime id.

    Tests launch ``garuda.acp.fake_agent`` in-process under many ad hoc runtime ids,
    and every ACP run checks the agent's reported mode against a table for its
    runtime (#732). For an id that is not a shipped harness this answers with the
    fake's own modes and the Claude Code and Codex mode names tests emulate.
    Shipped harness ids keep their real tables, or none. Worker processes do not
    see this; their tests classify the fake with a ``modes:`` runtimes entry.
    """

    def __init__(self, tables, shipped):
        super().__init__(tables)
        self._shipped = frozenset(shipped)
        self._fallback = None

    def get(self, key, default=None):
        if key in self or key in self._shipped:
            return super().get(key, default)
        if self._fallback is None:
            from garuda.acp.fake_agent import MODE_AUTHORITY

            emulated = {}
            for runtime in ("claude", "codex"):
                emulated.update(super().get(runtime) or {})
            self._fallback = {**emulated, **MODE_AUTHORITY}
        return self._fallback


@pytest.fixture(autouse=True)
def _fake_harness_modes(monkeypatch):
    """A test of an unclassified harness uses a shipped id, or replaces
    ``roles.MODE_AUTHORITY`` with a plain dict."""
    from garuda.acp.catalog import builtin_manifest_dicts
    from garuda.runtime import roles

    shipped = {item["runtime_id"] for item in builtin_manifest_dicts()} | {"native"}
    monkeypatch.setattr(roles, "MODE_AUTHORITY", _FakeModeTables(roles.MODE_AUTHORITY, shipped))


@pytest.fixture(autouse=True)
def _isolate_garuda_home(tmp_path_factory, monkeypatch):
    """Give settings, sessions, leases and derived recovery a fresh test owner.

    Tests that need a specific leases or recovery directory still override it.
    """
    monkeypatch.setenv(
        "GARUDA_SESSIONS_DIR", str(tmp_path_factory.mktemp("garuda-sessions"))
    )
    monkeypatch.setenv(
        "GARUDA_GLOBAL_SETTINGS",
        str(tmp_path_factory.mktemp("garuda-settings") / "settings.yaml"),
    )
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path_factory.mktemp("garuda-leases")))
    monkeypatch.delenv("GARUDA_RECOVERY_ROOT", raising=False)


@pytest.fixture
def fake_acp_confinement(monkeypatch):
    """Exercise ACP wiring with fake adapters and a simulated Docker boundary."""
    from garuda.acp import catalog
    from garuda.config import garuda_yaml as gy
    from garuda.workspace import confined_acp as ca

    which = ca.shutil.which
    make_adapter = catalog.adapter_for_manifest
    checked = {}

    def preflight(workspace, confinement):
        assert confinement.image == "fake-acp-image"
        checked[workspace] = confinement

    def adapter(manifest, *, argv_override=None, cwd=None, **kwargs):
        if argv_override is None:
            return make_adapter(manifest, cwd=cwd, **kwargs)
        assert cwd == ca.CONTAINER_WORKSPACE and "--read-only" in argv_override
        mount = next(arg for arg in argv_override if arg.endswith(f":{cwd}:ro"))
        workspace = mount.removesuffix(f":{cwd}:ro")
        assert workspace in checked
        image = checked[workspace].image
        command = argv_override[argv_override.index(image) + 1:]
        assert tuple(command) == checked[workspace].command
        return make_adapter(manifest, argv_override=command, cwd=workspace, **kwargs)

    monkeypatch.setattr(ca.shutil, "which", lambda cmd: "/fake/docker" if cmd == "docker" else which(cmd))
    monkeypatch.setattr(ca, "preflight", preflight)
    monkeypatch.setattr(catalog, "adapter_for_manifest", adapter)

    def configure(*runtime_ids, command="fake-acp-shim"):
        user_file = gy.user_path()
        doc = gy.load_file(user_file) or {"version": 1}
        for runtime_id in runtime_ids:
            doc.setdefault("harnesses", {}).setdefault(runtime_id, {})["confinement"] = {
                "image": "fake-acp-image", "command": [command]}
        user_file.write_text(gy.dump(doc))

    return configure
