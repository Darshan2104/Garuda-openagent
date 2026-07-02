import pytest


@pytest.fixture(autouse=True)
def _isolate_garuda_home(tmp_path_factory, monkeypatch):
    """Keep tests away from ~/.garuda: sessions go to a temp dir and the
    global hooks settings file is pointed at a nonexistent temp path."""
    monkeypatch.setenv(
        "GARUDA_SESSIONS_DIR", str(tmp_path_factory.mktemp("garuda-sessions"))
    )
    monkeypatch.setenv(
        "GARUDA_GLOBAL_SETTINGS",
        str(tmp_path_factory.mktemp("garuda-settings") / "settings.yaml"),
    )
