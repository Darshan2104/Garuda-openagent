"""Redaction and validation tests for issue #19 (P0.11).

Known-secret, oversize, path-escape, and false-positive fixtures: secrets never
reach generated files, unsafe handoffs block with every violation listed, and
redaction touches only pack text — never repository documentation.
"""

import pytest

from garuda.context.pack import ContextPackManager, compile_handoff, sync_context_pack
from garuda.context.redact import (
    UnsafeHandoffError,
    assert_safe_for_switch,
    redact_pack,
    redact_text,
    summarize_command_output,
    validate_pack,
)
from garuda.context.schemas import parse_handoff
from garuda.context.state_card import WorkingState


def _state() -> WorkingState:
    state = WorkingState(task="Rotate keys")
    state.note_check("pytest -q", 0, turn=1)
    return state


def test_known_secrets_are_redacted_with_kinds():
    text = "key sk-abc123XYZ_, ghp_deadbeefcafe1234, and AKIAIOSFODNN7EXAMPLE"
    cleaned, findings = redact_text(text)
    assert "sk-abc123" not in cleaned
    assert "ghp_deadbeef" not in cleaned
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned
    assert cleaned.count("[REDACTED:") == 3
    kinds = {f.kind for f in findings}
    assert {"api-token", "aws-key"} <= kinds


def test_assignment_and_bearer_patterns():
    cleaned, _ = redact_text("password=hunter2 hunter2\nAuthorization: Bearer abcdef123456")
    assert "hunter2 hunter2" not in cleaned
    assert "abcdef123456" not in cleaned


def test_false_positives_stay_readable():
    prose = "The token bucket refills every minute; no secret handshake required."
    cleaned, findings = redact_text(prose)
    assert cleaned == prose
    assert findings == []


def test_oversize_path_escape_and_reasoning_block():
    result = validate_pack({}, "x" * 32769)
    assert not result.valid
    assert any("exceeds bound" in e for e in result.errors)

    result = validate_pack({}, "ok", changed_files=("../escape.txt", "/abs.txt"))
    assert not result.valid
    assert sum("workspace" in e for e in result.errors) == 2

    result = validate_pack({}, "My <thinking>step by step</thinking> plan")
    assert not result.valid
    assert any("no-reasoning" in e for e in result.errors)

    with pytest.raises(UnsafeHandoffError, match="unsafe handoff blocks switching"):
        assert_safe_for_switch({}, "x" * 32769, changed_files=("../e",))


def test_writer_redacts_secrets_and_marks_handoff(tmp_path):
    manager = ContextPackManager(tmp_path / ".context")
    doc, body = compile_handoff(
        _state(), source_runtime="native", session_id="g", native_session_id="n"
    )
    doc, body = (
        doc,
        body + "\nDeploy with api_key=supersecretvalue9.\n",
    )
    target = manager.write_handoff(doc, body)
    written = target.read_text(encoding="utf-8")
    assert "supersecretvalue9" not in written
    assert "[REDACTED:credential]" in written
    parsed, _ = parse_handoff(written)
    assert parsed.redacted is True


def test_writer_blocks_unsafe_handoff_leaving_no_file(tmp_path):
    from dataclasses import replace

    manager = ContextPackManager(tmp_path / ".context")
    doc, body = compile_handoff(_state(), source_runtime="native")
    doc = replace(doc, changed_files=("../escape.txt",))
    with pytest.raises(UnsafeHandoffError, match="workspace"):
        manager.write_handoff(doc, body)
    assert not manager.handoff_path.exists()


def test_redaction_never_touches_repository_files(tmp_path):
    durable = tmp_path / "notes.md"
    durable.write_text("password=hunter2\n", encoding="utf-8")
    manager = ContextPackManager(tmp_path / ".context")
    doc, body = compile_handoff(_state(), source_runtime="native")
    manager.write_handoff(doc, body + "nothing secret here")
    assert durable.read_text(encoding="utf-8") == "password=hunter2\n"


def test_command_output_summaries_keep_head_and_tail():
    output = "\n".join(f"line {i}" for i in range(500))
    summary = summarize_command_output(output, limit=200)
    assert len(summary) < len(output)
    assert "line 0" in summary
    assert "line 499" in summary
    assert "clipped" in summary
    assert summarize_command_output("short") == "short"


def test_redact_pack_preserves_structure():
    front = {"task": "T", "count": 3, "flag": True, "note": "token=abc123XYZ"}
    cleaned_front, cleaned_body, findings = redact_pack(front, "clean body")
    assert cleaned_front["count"] == 3
    assert cleaned_front["flag"] is True
    assert "abc123XYZ" not in cleaned_front["note"]
    assert cleaned_body == "clean body"
    assert findings and findings[0].kind == "credential"


def test_full_pem_block_is_redacted_not_just_header():
    body = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA7bq3fakesecretbody123\n"
        "MOREFAKEKEYMATERIAL456\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    cleaned, findings = redact_text(body)
    assert "MIIEpAIBAAKCAQEA7bq3fakesecretbody123" not in cleaned
    assert "MOREFAKEKEYMATERIAL456" not in cleaned
    assert "BEGIN RSA PRIVATE KEY" not in cleaned
    assert "END RSA PRIVATE KEY" not in cleaned
    assert "[REDACTED:private-key]" in cleaned
    assert any(f.kind == "private-key" for f in findings)


def test_nested_unknown_fields_and_task_are_scrubbed_at_writer(tmp_path):
    from dataclasses import replace

    manager = ContextPackManager(tmp_path / ".context")
    doc, body = compile_handoff(_state(), source_runtime="native")
    # Secrets in scalar task, session IDs, and nested unknown frontmatter must
    # not survive the writer: the old `_scrub` rebuilt from the original object.
    password_label = "".join(("pass", "word"))
    nested_value = "nestedhunter99"
    list_value = "listsupersecret1"
    task_value = "tasksecret77"
    nested_secret = {
        "outer": {"inner": f"{password_label}={nested_value}"},
        "items": ["clean", f"token={list_value}"],
    }
    doc = replace(
        doc,
        task=f"Rotate keys {password_label}={task_value}",
        garuda_session_id="g1",
        native_session_id="n1",
        extra={"mystery": nested_secret, "plain": "ok"},
    )
    target = manager.write_handoff(doc, body)
    written = target.read_text(encoding="utf-8")
    assert task_value not in written
    assert nested_value not in written
    assert list_value not in written
    assert "[REDACTED:credential]" in written
    parsed, _ = parse_handoff(written)
    assert task_value not in parsed.task
    assert nested_value not in str(parsed.extra.get("mystery", ""))
    assert parsed.redacted is True


@pytest.mark.parametrize("credential,value", [
    ("DB_PASSWORD=", "hunter2hunter2"),
    ("AWS_SECRET_ACCESS_KEY=", "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"),
    ("OPENAI_API_KEY: ", "opaque-provider-credential"),
    ("GITHUB_TOKEN=", "opaque-github-credential"),
    ("HF_TOKEN=", "hf_abcdefghijklmnopqrstuvwxyz"),
    ("AWS_ACCESS_KEY_ID=", "opaque-access-credential"),
    ("", "hf_abcdefghijklmnopqrstuvwxyz012345"),
    ("", "sk-ant-abcdefghijklmnopqrstuvwxyz"),
])
def test_generated_packs_redact_environment_credentials(tmp_path, credential, value):
    """Failed commands and check evidence must not persist credentials (#483)."""
    state = WorkingState(task="fix login")
    assignment = credential + value
    state.note_check(assignment + " pytest tests/test_login.py", 0, turn=3)
    state.note_failure("bash", "env output: " + assignment)
    paths = sync_context_pack(ContextPackManager(tmp_path), state, session_id="s1")
    for path in paths.values():
        written = path.read_text()
        assert value not in written
        assert "Redaction:" in written
        assert "pytest tests/test_login.py" in written


@pytest.mark.parametrize("text,secret", [
    ('config {"api_key": "opaque-json-value"}', "opaque-json-value"),
    ('"password": "hunter2hunter2"', "hunter2hunter2"),
    ("password = 'hunter2 hunter3'", "hunter3"),
    ("https://user:p4ssw0rd123@host/repo", "p4ssw0rd123"),
    ("postgres://admin:SuperSecret99@db:5432/app", "SuperSecret99"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("DB_PASS=opaque-pass", "opaque-pass"),
    ("MYSQL_PWD=opaque-pwd", "opaque-pwd"),
    ("PRIVATE_KEY=opaque-private-key", "opaque-private-key"),
    ("x-api-key: opaque-header-key", "opaque-header-key"),
    ("--password opaque-flag-value", "opaque-flag-value"),
    ("ghs_abcdefghijklmnop", "ghs_abcdefghijklmnop"),
    ("ghu_abcdefghijklmnop", "ghu_abcdefghijklmnop"),
    ("ghr_abcdefghijklmnop", "ghr_abcdefghijklmnop"),
    ("glpat-abcdefghijklmnopqrst", "glpat-abcdefghijklmnopqrst"),
    ("AIzaSyA-abcdefghijklmnopqrstuvwxyz12345", "AIzaSyA-abcdefghijklmnopqrstuvwxyz12345"),
    ("npm_abcdefghijklmnopqrstuvwxyz0123456789", "npm_abcdefghijklmnopqrstuvwxyz0123456789"),
])
def test_review_generated_pack_credential_formats(tmp_path, text, secret):
    state = WorkingState(task="fix login")
    state.note_failure("bash", text)
    for path in sync_context_pack(ContextPackManager(tmp_path), state, session_id="review").values():
        assert secret not in path.read_text()
        assert "Redaction:" in path.read_text()


@pytest.mark.parametrize("text", [
    "max_tokens=4096", "num_tokens: 1234", "tokenizer: cl100k_base",
    "secret_scanning: enabled", "password_hash == expected",
    "AssertionError: token: expected 3", "hf_transfer",
])
def test_review_redaction_preserves_nonsecret_diagnostics(text):
    assert redact_text(text) == (text, [])


# Regression corpus for issue #870: the credential pattern matched any word
# ending in `pass`/`secret`/`token` before `:` and dropped words from plans,
# reviews and briefs. Every secret below is fake.
_PEM = ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEFAKEFAKE\n"
        "-----END OPENSSH PRIVATE KEY-----")

TRUE_POSITIVES = [
    # (text, the fake secret that must not survive)
    ("key sk-abc123XYZ_ here", "sk-abc123XYZ_"),
    ("ghp_deadbeefcafe1234", "ghp_deadbeefcafe1234"),
    ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("password=hunter2 hunter2", "hunter2 hunter2"),
    ("Authorization: Bearer abcdef123456", "abcdef123456"),
    ("Deploy with api_key=supersecretvalue9.", "supersecretvalue9"),
    ("note token=abc123XYZ", "abc123XYZ"),
    ("acp token=lanesecret", "lanesecret"),
    (_PEM, "b3BlbnNzaC1rZXktdjEFAKEFAKE"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEtruncatedFAKE", "BEGIN RSA PRIVATE KEY"),
    ("DB_PASSWORD=hunter2hunter2", "hunter2hunter2"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
     "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"),
    ("OPENAI_API_KEY: opaque-provider-credential", "opaque-provider-credential"),
    ("GITHUB_TOKEN=opaque-github-credential", "opaque-github-credential"),
    ("AWS_ACCESS_KEY_ID=opaque-access-credential", "opaque-access-credential"),
    ("hf_abcdefghijklmnopqrstuvwxyz012345", "hf_abcdefghijklmnopqrstuvwxyz012345"),
    ("sk-ant-api03-FAKEabcdefghijklmnop", "FAKEabcdefghijklmnop"),
    ('config {"api_key": "opaque-json-value"}', "opaque-json-value"),
    ('"password": "hunter2hunter2"', "hunter2hunter2"),
    ("password = 'hunter2 hunter3'", "hunter3"),
    ("https://user:p4ssw0rd123@host/repo", "p4ssw0rd123"),
    ("postgres://admin:SuperSecret99@db:5432/app", "SuperSecret99"),
    ("mongodb+srv://svc:FAKEmongo42@cluster0.example.net/db", "FAKEmongo42"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("DB_PASS=opaque-pass", "opaque-pass"),
    ("MYSQL_PWD=opaque-pwd", "opaque-pwd"),
    ("PRIVATE_KEY=opaque-private-key", "opaque-private-key"),
    ("x-api-key: opaque-header-key", "opaque-header-key"),
    ("--password opaque-flag-value", "opaque-flag-value"),
    ("mysql --password=hunter2flag -u root", "hunter2flag"),
    ("ghs_abcdefghijklmnop", "ghs_abcdefghijklmnop"),
    ("glpat-abcdefghijklmnopqrst", "glpat-abcdefghijklmnopqrst"),
    ("AIzaSyA-abcdefghijklmnopqrstuvwxyz12345", "AIzaSyA-abcdefghijklmnopqrstuvwxyz12345"),
    ("npm_abcdefghijklmnopqrstuvwxyz0123456789", "npm_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("xoxb-1234-FAKEslackbot", "xoxb-1234-FAKEslackbot"),
    ("password: hunter22", "hunter22"),
    ("PASSWORD: hunter2hunter2", "hunter2hunter2"),
    ("PGPASSWORD=fakepgpass psql -h db", "fakepgpass"),
    ("PASS=hunter2", "hunter2"),
    ("export SECRET=opaque-env-secret", "opaque-env-secret"),
    ("  password: fakeyamlpass\n  user: app", "fakeyamlpass"),
    ("token: FAKEab12cd34EF", "FAKEab12cd34EF"),
    ("secret: 'rotate-me-now'", "rotate-me-now"),
    ('pass: "fakequoted"', "fakequoted"),
    ("pwd=Secret123x&user=me", "Secret123x"),
    ('self.password = "hunter2"', "hunter2"),
    ('password: str = "FAKEannotated"', "FAKEannotated"),
    ('const apiKey = "opaque-js-key";', "opaque-js-key"),
    ("dbPassword: fakecamelvalue", "fakecamelvalue"),
    ('client_secret: "opaque-oauth-secret"', "opaque-oauth-secret"),
    ("refresh_token=opaque-refresh-value", "opaque-refresh-value"),
    ("x-auth-token: opaque-header-token", "opaque-header-token"),
    ("$password = 'fakephp';", "fakephp"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.FAKEsig", "eyJhbGciOiJIUzI1NiJ9"),
    ('SECRET_KEY = "django-insecure-FAKEkey"', "django-insecure-FAKEkey"),
    ("password=changeme", "changeme"),
    ("tests pass: token=FAKEchained1", "FAKEchained1"),
]

TRUE_NEGATIVES = [
    # The live and reproduced examples from issue #870.
    "...the tests pass: `sub` now returns `a - b`...",
    "Verification: tests pass: 3 passed",
    "def run(bypass: bool = False):",
    "secret: rotate this",
    "checks pass: yes",
    # Plan and review text.
    "1. Run pytest; expect: all tests pass: 12 passed, 0 failed.",
    "Verification: `python -m pytest -q tests/test_textkit.py` -> tests pass: 5 passed",
    "Finding (minor): the bypass: flag is never read in cli.py.",
    "Review: approve. Lint and tests pass: ruff clean, mypy clean.",
    "Risk: the token bucket refills every minute; no secret handshake required.",
    "Plan: add a passport: field to the user model and a compass: heading.",
    "The `password:` field is masked in the form.",
    "Uses bearer authentication for the admin API.",
    "| pass: 3 | fail: 0 | skip: 1 |",
    "PASS: tests/shell/test_install.sh",
    "XPASS: test_flaky_upload",
    "3 passed, 1 skipped in 0.42s",
    # Python.
    "password: str",
    "def connect(user: str, password: Optional[str] = None) -> Conn:",
    "password = getpass()",
    "password = getpass.getpass('Password: ')",
    'password = os.environ["DB_PASSWORD"]',
    'api_key = os.getenv("OPENAI_API_KEY")',
    "password_hash = hash(password)",
    "if password == expected:",
    "login(user=user, password=password)",
    "token = tokens[0]",
    "token = None",
    "secret_key = settings.SECRET_KEY",
    "max_tokens=4096",
    "num_tokens: 1234",
    "tokenizer: cl100k_base",
    "password_hash == expected",
    "AssertionError: token: expected 3",
    "hf_transfer",
    # JavaScript / TypeScript.
    "const token = process.env.GITHUB_TOKEN;",
    "interface Login { password: string; remember: boolean }",
    "headers: { Authorization: `Bearer ${token}` },",
    "return { token: req.headers.authorization };",
    "apiKey: config.apiKey,",
    "if (!token) return;",
    "npm_lifecycle_event",
    # YAML and shell.
    "secret_scanning: enabled",
    "password: ${DB_PASSWORD}",
    "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
    "api_key: null",
    "bypass: true",
    "PWD=/home/me/projects/app",
    "export PASSWORD=$DB_PASSWORD",
    'password: "<your-password>"',
    "passed: 42",
    "token_count: 512",
    '<input type="password" name="pass">',
]


def test_corpus_sizes():
    assert len(TRUE_POSITIVES) >= 40 and len(TRUE_NEGATIVES) >= 40


@pytest.mark.parametrize("text,secret", TRUE_POSITIVES)
def test_corpus_true_positive_is_redacted(text, secret):
    cleaned, findings = redact_text(text)
    assert secret not in cleaned
    assert findings and "[REDACTED:" in cleaned


@pytest.mark.parametrize("text", TRUE_NEGATIVES)
def test_corpus_true_negative_is_left_alone(text):
    assert redact_text(text) == (text, [])


def test_rejected_match_keeps_the_rest_of_the_line():
    cleaned, findings = redact_text("Verification: tests pass: 3 passed; DB_PASSWORD=hunter2hunter2")
    assert cleaned == "Verification: tests pass: 3 passed; [REDACTED:credential]"
    assert [f.kind for f in findings] == ["credential"]


def test_redacted_text_is_stable_when_redacted_again():
    """Briefs re-redact stored artifacts (#816): a second pass changes nothing."""
    once, _ = redact_text("password=hunter2 and tests pass: 3 passed")
    assert redact_text(once) == (once, [])
