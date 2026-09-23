"""Docs contract checks for issue #9 (P0.3).

CI fails on broken local docs links and unexpected nested README files, and
release docs must not claim a hand-maintained test count. The checks never
fetch remote URLs. Fixture tests below prove each check can fail; the
``test_repo_*`` tests run the same logic against the real repository, which is
also exercised by ``python scripts/check_docs.py`` in CI.
"""

import importlib.util
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKER_PATH = REPO_ROOT / "scripts" / "check_docs.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_docs", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def test_broken_link_fixture_fails():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "a.md").write_text("[missing](missing.md)\n")
        broken = checker.find_broken_local_links(root)
        assert len(broken) == 1
        assert "a.md -> missing.md" in broken[0]


def test_valid_relative_link_fixture_passes(tmp_path):
    (tmp_path / "b.md").write_text("# B\n")
    (tmp_path / "a.md").write_text("[b](b.md) [section](b.md#section)\n")
    assert checker.find_broken_local_links(tmp_path) == []


def test_remote_links_are_skipped_never_fetched(tmp_path):
    (tmp_path / "a.md").write_text(
        "[web](https://example.com/does-not-exist-12345)\n"
        "[mail](mailto:someone@example.com)\n"
        "[anchor](#section)\n"
    )
    assert checker.find_broken_local_links(tmp_path) == []


def test_nested_readme_fixture_fails(tmp_path):
    (tmp_path / "README.md").write_text("# root\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "README.md").write_text("# nested\n")
    assert checker.find_nested_readmes(tmp_path) == ["sub/README.md"]


def test_root_readme_only_passes(tmp_path):
    (tmp_path / "README.md").write_text("# root\n")
    assert checker.find_nested_readmes(tmp_path) == []


def test_test_count_claim_fixture_fails(tmp_path):
    (tmp_path / "README.md").write_text("With 1131 tests passing throughout.\n")
    claims = checker.find_test_count_claims(tmp_path)
    assert len(claims) == 1
    assert "1131 tests" in claims[0]


def test_archive_test_counts_are_excluded(tmp_path):
    archived = tmp_path / "docs" / "archive"
    archived.mkdir(parents=True)
    (archived / "old.md").write_text("504 tests passing.\n")
    assert checker.find_test_count_claims(tmp_path) == []


def test_repo_has_no_broken_local_links():
    assert checker.find_broken_local_links(REPO_ROOT) == []


def test_repo_has_no_nested_readmes():
    assert checker.find_nested_readmes(REPO_ROOT) == []


def test_release_docs_have_no_test_count_claims():
    assert checker.find_test_count_claims(REPO_ROOT) == []
