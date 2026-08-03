"""Regressions for defects found by driving the shipped interfaces end to end.

Each of these was reachable from a documented command and invisible to the suite
at the time, because the suite tested the unit and the defect lived in the wiring
between units. Where a fix has a live-observed symptom, it is named.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from garuda.core.tool_runner import _missing_required_arguments
from garuda.tools.search import GlobTool

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestVersionReporting:
    def test_package_version_matches_pyproject(self):
        """`health` reported 1.1.0 while the code was 1.1.1.

        It read `importlib.metadata.version`, which under `pip install -e .` is a
        snapshot from install time. A version field that lags the running code is
        worse than none: it is what a client checks to decide whether a fix is
        deployed.
        """
        import garuda

        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert garuda.__version__ == pyproject["project"]["version"]

    @pytest.mark.asyncio
    async def test_health_reports_the_running_version(self):
        import garuda
        from garuda.interfaces.server import JsonRpcServer, ServerConfig

        server = JsonRpcServer(ServerConfig(token="t"))
        assert (await server._health())["version"] == garuda.__version__


class TestServeBannerIsFlushed:
    def test_generated_token_reaches_a_redirected_stdout(self):
        """The token print was block-buffered, so `garuda serve > log` stranded it.

        The process then blocks in the event loop forever: the buffer never fills,
        never flushes, and every request 401s against a token nobody can read. This
        runs the real banner path in a subprocess with stdout as a pipe — the shape
        that failed — and asserts the token is readable before the process ends.
        """
        code = (
            "from garuda.interfaces.server import ServerConfig, ensure_secure_config\n"
            "c = ServerConfig(host='127.0.0.1')\n"
            "ensure_secure_config(c)\n"
            "import time; time.sleep(0.2)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=REPO_ROOT,
        )
        assert "Authorization: Bearer" in proc.stdout, proc.stderr


class TestMissingRequiredArguments:
    def test_a_mistyped_key_names_what_was_actually_sent(self):
        """Observed live: a model sent `{"pattern:": "*.py"}` — colon inside the key
        — and got `KeyError: 'pattern'`, which names the key it believed it had sent
        and says nothing about the one it used. It burned a turn on that."""
        message = _missing_required_arguments(GlobTool(), {"pattern:": "*.py", "path": ""})
        assert message is not None
        assert "missing required argument(s): pattern" in message
        assert "pattern:" in message, "must echo the key that was actually received"

    def test_a_valid_call_is_not_intercepted(self):
        assert _missing_required_arguments(GlobTool(), {"pattern": "*.py"}) is None

    def test_optional_arguments_are_not_required(self):
        from garuda.tools.files import ReadFileTool

        assert _missing_required_arguments(ReadFileTool(), {"path": "a.py"}) is None

    def test_a_non_object_argument_payload_is_reported_not_raised(self):
        message = _missing_required_arguments(GlobTool(), "*.py")
        assert message is not None and "JSON object" in message

    def test_a_tool_declaring_nothing_required_always_passes(self):
        class _Bare:
            name = "bare"
            parameters = {"type": "object", "properties": {}}

        assert _missing_required_arguments(_Bare(), {}) is None

    @pytest.mark.asyncio
    async def test_runner_returns_the_message_as_a_tool_error(self):
        """It must be an error result the model can read, not a raised exception:
        a malformed call is the model's to fix next turn, not a run-ending fault."""
        from garuda.context.manager import ContextManager
        from garuda.core.events import EventStore
        from garuda.core.permissions import PermissionEngine
        from garuda.core.tool_runner import ToolRunner
        from garuda.model.script_model import ScriptModel
        from garuda.plugins.hooks import HookRegistry
        from garuda.types import ToolCall
        from garuda.workspace.local import LocalEnvironment

        tool = GlobTool()
        runner = ToolRunner(
            tool_map={tool.name: tool},
            env=LocalEnvironment(workspace_root="."),
            ctx=None,
            context=ContextManager(model=ScriptModel(responses=[])),
            permissions=PermissionEngine(mode="yolo"),
            hooks=HookRegistry(),
            events=EventStore(),
        )
        result = await runner.execute(
            ToolCall(id="c1", name="glob", arguments={"pattern:": "*.py"})
        )
        assert result.is_error is True
        assert result.tool_call_id == "c1"
        assert "missing required argument(s)" in result.content


class TestBinaryReadIsPortable:
    """`read_pdf` / `read_spreadsheet` were dead on every macOS host.

    They ran `base64 -w0 FILE` with a bare `base64 FILE` fallback. BSD/macOS
    `base64` accepts no positional input file at all — both forms fail with
    `base64: invalid argument <path>` — so the fallback was as wrong as the primary
    and the tools reported a present file as missing. Reads from stdin now, the one
    form GNU and BSD agree on.
    """

    @pytest.mark.asyncio
    async def test_bytes_round_trip_through_the_environment(self, tmp_path):
        from garuda.tools.documents import _read_file_bytes
        from garuda.workspace.local import LocalEnvironment

        payload = bytes(range(256)) * 8  # non-UTF8, spans every byte value
        (tmp_path / "blob.bin").write_bytes(payload)
        env = LocalEnvironment(workspace_root=str(tmp_path))
        assert await _read_file_bytes(env, "blob.bin") == payload

    @pytest.mark.asyncio
    async def test_line_wrapping_does_not_corrupt_the_decode(self, tmp_path):
        """No `-w0` is passed, so output arrives wrapped; b64decode must tolerate it.
        Large enough to force several wraps at BSD's 76-column default."""
        from garuda.tools.documents import _read_file_bytes
        from garuda.workspace.local import LocalEnvironment

        payload = b"".join(bytes([i % 256]) for i in range(5000))
        (tmp_path / "big.bin").write_bytes(payload)
        env = LocalEnvironment(workspace_root=str(tmp_path))
        assert await _read_file_bytes(env, "big.bin") == payload

    @pytest.mark.asyncio
    async def test_a_missing_file_still_raises_filenotfound(self, tmp_path):
        from garuda.tools.documents import _read_file_bytes
        from garuda.workspace.local import LocalEnvironment

        env = LocalEnvironment(workspace_root=str(tmp_path))
        with pytest.raises(FileNotFoundError):
            await _read_file_bytes(env, "absent.bin")

    @pytest.mark.asyncio
    async def test_read_spreadsheet_reads_a_real_workbook(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        from garuda.tools.documents import ReadSpreadsheetTool
        from garuda.workspace.local import LocalEnvironment

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["product", "units"])
        sheet.append(["gizmo", 11])
        workbook.save(tmp_path / "r.xlsx")

        result = await ReadSpreadsheetTool().execute(
            {"path": "r.xlsx"}, LocalEnvironment(workspace_root=str(tmp_path)), None
        )
        assert result.is_error is False
        assert "gizmo" in result.content and "11" in result.content


class TestReadonlyPermitsReads:
    """`readonly` denied *every* command, which made it unusable rather than safe.

    `plan`, `explore` and `reviewer` all grant `bash`, so the tool they are built
    around could never run — and the completion gate re-runs the agent's own
    verification commands, which were denied too. Observed live: an `explore`
    subagent spent all 50 turns on "permission issues with verification commands"
    and failed with the answer already in hand. After the fix the same task
    succeeded in 4 turns.
    """

    @pytest.fixture
    def engine(self):
        from garuda.core.permissions import PermissionEngine

        return PermissionEngine(mode="readonly")

    @pytest.mark.parametrize(
        "command",
        ["ls -la", "cat calc.py", "grep -rn foo .", "head -5 x", "wc -l x", "diff a b"],
    )
    def test_inspection_commands_are_allowed(self, engine, command):
        from garuda.core.permissions import PermissionDecision

        assert engine.check_command(command) == PermissionDecision.ALLOW

    @pytest.mark.parametrize(
        "command",
        [
            "echo x > f",  # redirect
            "rm -rf build",  # unrecognised program
            "touch f",
            "python x.py",  # every interpreter is assumed to write
            "python -m pytest -q",
            "make",
            "cat a | tee f",  # tee
            "sudo ls",  # sudo
            "ls & rm -rf x",  # backgrounding escapes the check
            "cat $(ls)",  # command substitution
        ],
    )
    def test_anything_that_could_write_is_still_denied(self, engine, command):
        """Fail-closed is the whole design: unrecognised means denied."""
        from garuda.core.permissions import PermissionDecision

        assert engine.check_command(command) == PermissionDecision.DENY

    def test_write_tools_are_unaffected(self, engine):
        from garuda.core.permissions import PermissionDecision

        for tool in ("write_file", "edit", "multi_edit"):
            assert engine.check_tool(tool) == PermissionDecision.DENY

    def test_a_redirect_denies_even_inside_quotes(self, engine):
        """Documents the conservative edge, so nobody "fixes" it into a hole.

        `split_segments` is quote-aware, but the mutating-syntax test runs on the raw
        segment, so a `>` inside a string literal still denies. `echo "cat a > b"`
        writes nothing and is refused anyway — a false *denial*, which costs the
        agent one rephrase, where the opposite error would let a redirect through.
        """
        from garuda.core.permissions import PermissionDecision

        assert engine.check_command('echo "cat a > b"') == PermissionDecision.DENY
        assert engine.check_command("cat a > b") == PermissionDecision.DENY
        # The plain form of the same read is allowed.
        assert engine.check_command("cat a") == PermissionDecision.ALLOW


class TestCustomToolsSurviveAProfileAllowlist:
    """Both documented extension points were silent no-ops on every shipped profile.

    `build` names 27 tools, so a tool added by `SoftwareAgent.register_tool` or by an
    opt-in `.agent/tools` module matched nothing and was filtered out — twice, once
    in `build_toolkit` and again in `run_state._filter_tools`. The project-tools path
    was the worse of the two: it logged "Loaded 1 project tool(s): hello" and then
    discarded it, so the log said the feature worked while the model never saw it.
    Verified live after the fix: `CALL hello {'name': 'World'}`.
    """

    @staticmethod
    def _tool(name="sdk_ping"):
        from garuda.types import ToolResult

        class _Custom:
            def __init__(self):
                self.name = name
                self.description = "custom"
                self.parameters = {"type": "object", "properties": {}, "required": []}

            async def execute(self, arguments, env, ctx):
                return ToolResult(tool_call_id="", content="ok")

        return _Custom()

    @pytest.mark.asyncio
    async def test_extra_tool_survives_a_restrictive_allowlist(self):
        from garuda.tools import build_toolkit

        tools, _ = await build_toolkit(
            ["bash", "read_file"], [], extra_tools=[self._tool()], workspace=None
        )
        assert "sdk_ping" in {t.name for t in tools}

    @pytest.mark.asyncio
    async def test_the_allowlist_still_restricts_discovered_tools(self):
        """The fix must be additive only — a profile still cannot get write_file."""
        from garuda.tools import build_toolkit

        tools, _ = await build_toolkit(["bash", "read_file"], [], workspace=None)
        assert {t.name for t in tools} == {"bash", "read_file"}

    @pytest.mark.asyncio
    async def test_the_second_filter_in_prepare_run_also_keeps_it(self):
        """`build_toolkit` surviving is not enough: `prepare_run` filters again, and
        that is where the tool was actually lost on the CLI path."""
        from garuda.core.run_state import _filter_tools
        from garuda.tools import build_toolkit

        tools, _ = await build_toolkit(
            ["bash"], [], extra_tools=[self._tool()], workspace=None
        )
        kept = {t.name for t in _filter_tools(tools, ["bash"])}
        assert "sdk_ping" in kept, "the explicit marker must survive both allowlists"
        assert "bash" in kept

    @pytest.mark.asyncio
    async def test_an_unmarked_tool_is_still_filtered(self):
        """Only *explicitly supplied* tools bypass the list, not anything present."""
        from garuda.core.run_state import _filter_tools

        assert _filter_tools([self._tool("stray")], ["bash"]) == []


class TestChatHonoursPermissionPosture:
    """`garuda chat` built its PermissionEngine from `profile.permission_mode`, so it
    ignored both the mode preset and (before it existed) the flag. `--mode readonly`
    left the agent able to write."""

    @pytest.mark.parametrize(
        "agent,mode,permission_mode,expected",
        [
            ("build", None, None, "smart"),
            ("build", "readonly", None, "readonly"),
            ("build", None, "yolo", "yolo"),
            # Documented precedence: an explicit flag is narrower than a posture.
            ("build", "readonly", "yolo", "yolo"),
            ("harbor", "readonly", None, "readonly"),
        ],
    )
    @pytest.mark.asyncio
    async def test_permission_precedence(self, agent, mode, permission_mode, expected):
        from garuda.interfaces.session import AgentSession

        session = await AgentSession.create(
            agent_name=agent,
            model="openai/gpt-4o-mini",
            workspace=".",
            mode=mode,
            permission_mode=permission_mode,
        )
        try:
            assert session.permissions.mode == expected
        finally:
            await session.close()

    def test_the_chat_parser_accepts_the_flag(self):
        """It was rejected outright — on the one interface built around permission
        prompts."""
        from garuda.interfaces.main import build_parser

        args = build_parser().parse_args(["chat", "--permission-mode", "readonly"])
        assert args.permission_mode == "readonly"


class TestExportedVersionsTrackThePackage:
    """Two version fields were literals that stopped matching the code.

    `serve`'s health read install-time metadata (1.1.0 while running 1.1.1) and the
    ATIF exporter had `agent_version="0.5.0"` hardcoded, stamping that into every
    graded trajectory — the field whose only job is saying which harness produced a
    number. Neither was compared to anything, so neither could be noticed.
    """

    def test_atif_agent_version_is_the_running_version(self):
        import garuda
        from garuda.eval.atif_export import events_to_atif

        trajectory = events_to_atif([], session_id="s1")
        assert trajectory["agent"]["version"] == garuda.__version__

    def test_atif_schema_version_is_pinned(self):
        """The format the exporter claims to emit; consumers key off it."""
        from garuda.eval.atif_export import events_to_atif

        assert events_to_atif([], session_id="s1")["schema_version"] == "ATIF-v1.7"

    def test_the_harbor_adapter_reports_the_running_version(self):
        """Harbor stores this against the score, so it is the worst field to let
        drift: a real trajectory recorded 1.1.0 while the code was 1.1.1."""
        import garuda
        from garuda.eval.harbor_adapter import GarudaHarborAgent

        assert GarudaHarborAgent.version(GarudaHarborAgent) == garuda.__version__
