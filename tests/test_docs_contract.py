"""Docs contract checks for issue #9 (P0.3).

CI fails on broken local docs links and unexpected nested README files, and
release docs must not claim a hand-maintained test count. The checks never
fetch remote URLs. Fixture tests below prove each check can fail; the
``test_repo_*`` tests run the same logic against the real repository, which is
also exercised by ``python scripts/check_docs.py`` in CI.
"""

import argparse
import builtins
import importlib.util
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKER_PATH = REPO_ROOT / "scripts" / "check_docs.py"
DIAGRAM_CHECKER_PATH = REPO_ROOT / "scripts" / "check_diagram_links.py"


def _load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_script("check_docs", CHECKER_PATH)
diagram_checker = _load_script("check_diagram_links", DIAGRAM_CHECKER_PATH)


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


def test_duplicate_table_row_fixture_fails(tmp_path):
    (tmp_path / "README.md").write_text(
        "| Command | Use |\n"
        "|---|---|\n"
        "| `garuda run` | Run a task. |\n"
        "| `garuda run` | Run a task. |\n"
    )
    duplicates = [
        error for error in checker.check(tmp_path) if error.startswith("duplicate table row:")
    ]
    assert len(duplicates) == 1
    assert "README.md:4" in duplicates[0]
    assert "first at line 3" in duplicates[0]


def test_duplicate_rows_are_scoped_to_one_table(tmp_path):
    (tmp_path / "README.md").write_text(
        "| Name | Value |\n"
        "|---|---|\n"
        "| same | row |\n"
        "\n"
        "| Name | Value |\n"
        "|---|---|\n"
        "| same | row |\n"
    )
    assert checker.find_duplicate_table_rows(tmp_path) == []


def _fixture_parser():
    parser = argparse.ArgumentParser(prog="garuda")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run")
    run.add_argument("--mode", choices=["readonly", "rigorous"])
    runtime = commands.add_parser("runtime")
    runtime_commands = runtime.add_subparsers(dest="runtime_command")
    handoff = runtime_commands.add_parser("handoff")
    handoff.add_argument("--session", required=True)
    handoff.add_argument("--to", required=True)
    handoff.add_argument("--confirm", action="store_true")
    return parser


def test_documented_command_validation_accepts_abbreviated_reference():
    errors = checker.validate_documented_command(
        "garuda runtime handoff [--session S] [--to R] [--confirm]",
        _fixture_parser(),
    )
    assert errors == []


def _demo_fixture_parser():
    parser = _fixture_parser()
    commands = checker._subparsers_action(parser)
    commands.add_parser("worker", help=argparse.SUPPRESS)
    commands.add_parser("web", aliases=["dashboard"])
    return parser


def test_public_command_paths_skip_hidden_commands_and_aliases():
    paths = checker.public_command_paths(_demo_fixture_parser())
    assert paths == [
        ("garuda", "run"),
        ("garuda", "runtime"),
        ("garuda", "runtime", "handoff"),
        ("garuda", "web"),
    ]


def test_command_missing_from_use_cases_and_cheat_sheet_is_reported(tmp_path):
    use_cases = tmp_path / "docs" / "use-cases"
    use_cases.mkdir(parents=True)
    (use_cases / "explore.md").write_text("```bash\ngaruda run --mode readonly\n```\n")
    reference = tmp_path / "docs" / "reference"
    reference.mkdir()
    (reference / "cli.md").write_text("```bash\ngaruda web\n```\n")
    (reference / "cheat-sheet.md").write_text(
        "```bash\ngaruda runtime handoff --session S --to R   # preview\n```\n"
    )

    missing = checker.find_undemonstrated_commands(tmp_path, _demo_fixture_parser())

    # The CLI reference alone doesn't count as a demonstration.
    assert missing == ["garuda web"]


def test_documented_command_validation_ignores_trailing_shell_comment():
    parser = _fixture_parser()
    assert checker.validate_documented_command(
        "garuda run --mode readonly   # what's this? a comment", parser
    ) == []
    assert "unknown option" in checker.validate_documented_command(
        "garuda run --missing   # still checked before the comment", parser
    )[0]


def test_documented_command_validation_rejects_unknown_command_flag_and_choice():
    parser = _fixture_parser()
    assert "unknown subcommand" in checker.validate_documented_command(
        "garuda missing", parser
    )[0]
    assert "unknown option" in checker.validate_documented_command(
        "garuda run --missing", parser
    )[0]
    assert "invalid choice" in checker.validate_documented_command(
        "garuda run --mode unsafe", parser
    )[0]


def test_command_extraction_covers_fences_inline_spans_and_wrapping(tmp_path):
    guide = tmp_path / "docs" / "guides" / "guide.md"
    guide.parent.mkdir(parents=True)
    guide.write_text(
        "```bash\n"
        "$ garuda run \\\n"
        "  --mode readonly\n"
        "```\n\n"
        "Use `garuda runtime\n"
        "handoff --confirm` after previewing it.\n"
        "Use ``garuda run --mode readonly`` for inspection.\n"
        "~~~console\n"
        "garuda sessions\n"
        "~~~\n"
    )
    commands = checker.find_documented_commands(tmp_path)
    assert [item.command for item in commands] == [
        "garuda run --mode readonly",
        "garuda runtime handoff --confirm",
        "garuda run --mode readonly",
        "garuda sessions",
    ]


def test_command_extraction_honors_explicit_opt_out(tmp_path):
    guide = tmp_path / "docs" / "guides" / "guide.md"
    guide.parent.mkdir(parents=True)
    guide.write_text(
        "```bash docs-command-ignore\n"
        "garuda proposed --future\n"
        "```\n"
        "Use `garuda <command> --help`. <!-- docs-command-ignore -->\n"
    )
    assert checker.find_documented_commands(tmp_path) == []


def test_command_extraction_includes_new_user_docs_and_excludes_plans(tmp_path):
    docs = tmp_path / "docs"
    plans = docs / "plans"
    tutorials = docs / "tutorials"
    plans.mkdir(parents=True)
    tutorials.mkdir()
    (docs / "index.md").write_text("Use `garuda run`.\n")
    (tutorials / "new.md").write_text("Use `garuda chat`.\n")
    (plans / "future.md").write_text("Use `garuda future`.\n")
    commands = checker.find_documented_commands(tmp_path)
    assert [item.command for item in commands] == ["garuda run", "garuda chat"]


def test_command_extraction_includes_every_builder_choice(tmp_path):
    guide = tmp_path / "docs" / "guides" / "builder.md"
    guide.parent.mkdir(parents=True)
    guide.write_text(
        '<div class="gb" data-garuda-builder data-command="garuda run" markdown>\n'
        '<button data-args="--mode readonly">Read</button>\n'
        '<button data-args="">Default</button>\n'
        "</div>\n"
    )
    commands = checker.find_documented_commands(tmp_path)
    assert [item.command for item in commands] == [
        "garuda run --mode readonly",
        "garuda run",
    ]


def test_invalid_builder_choice_is_reported(tmp_path):
    guide = tmp_path / "docs" / "guides" / "builder.md"
    guide.parent.mkdir(parents=True)
    guide.write_text(
        '<div data-garuda-builder data-command="garuda run">\n'
        '<button data-args="--mode unsafe">Unsafe</button>\n'
        "</div>\n"
    )
    errors = checker.find_invalid_documented_commands(tmp_path, _fixture_parser())
    assert len(errors) == 1
    assert "builder.md:2: invalid choice 'unsafe'" in errors[0]


def test_command_mode_fails_loudly_without_installed_parser(tmp_path, monkeypatch, capsys):
    original_import = builtins.__import__

    def reject_garuda(name, *args, **kwargs):
        if name == "garuda.interfaces.main":
            raise ImportError("package unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_garuda)
    assert checker.main(["--root", str(tmp_path), "--commands"]) == 2
    assert "cannot import Garuda parser" in capsys.readouterr().err


def test_repo_has_no_broken_local_links():
    assert checker.find_broken_local_links(REPO_ROOT) == []


def test_repo_has_no_nested_readmes():
    assert checker.find_nested_readmes(REPO_ROOT) == []


def test_release_docs_have_no_test_count_claims():
    assert checker.find_test_count_claims(REPO_ROOT) == []


def test_release_docs_have_no_duplicate_table_rows():
    assert checker.find_duplicate_table_rows(REPO_ROOT) == []


def _write_site_page(site, rel, body):
    page = site / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(body)


def _mermaid(*clicks):
    lines = "\n".join(f'  click n{i} &quot;{target}&quot;' for i, target in enumerate(clicks))
    return f'<pre class="mermaid"><code>flowchart LR\n{lines}\n</code></pre>'


def test_diagram_links_resolve_like_a_browser(tmp_path):
    _write_site_page(tmp_path, "index.html", _mermaid("guide/#setup", "#local", "https://example.com/x"))
    _write_site_page(tmp_path, "guide/index.html", '<h2 id="setup">Setup</h2>')
    _write_site_page(
        tmp_path,
        "use-cases/index.html",
        '<h2 id="local">Local</h2>' + _mermaid("explore/#ask", "../guide/#setup"),
    )
    _write_site_page(tmp_path, "use-cases/explore/index.html", '<h2 id="ask">Ask</h2>')
    assert diagram_checker.find_broken_diagram_links(tmp_path) == [
        "index.html: diagram link '#local' has no anchor #local"
    ]


def test_diagram_link_to_missing_page_fails(tmp_path):
    _write_site_page(tmp_path, "index.html", _mermaid("missing/"))
    assert diagram_checker.find_broken_diagram_links(tmp_path) == [
        "index.html: diagram link 'missing/' has no page"
    ]
