import pytest


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
