"""`evidence.is_side_effect_free`: which verification commands may run concurrently.

A different axis from the discriminating-power classification in the same module.
This one gates concurrency, so it is an allowlist and it fails closed: being wrong
means two commands racing at the completion gate, while being conservative only
means they run one after another as they always did.
"""

import shutil

import pytest

from garuda.core import evidence
from garuda.core.permissions import PermissionEngine
from garuda.core.verifier import _verification_segments, group_command
from garuda.tools.bash import BashTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment

READ_ONLY = [
    "cat out.txt",
    "ls -la /app",
    "wc -l results.csv",
    "diff -u expected.txt actual.txt",
    "cmp a.bin b.bin",
    "grep -q EXPECTED out.txt",
    "cat out.txt | grep -q EXPECTED",
    "sha256sum artifact.tar",
    "jq -e '.ok' report.json",
    "test -f build/app",
    "LANG=C timeout 5 cat x",
    "/usr/bin/cat x",
    "xxd -l 32 -c8 out.txt",
    "xxd -r -s -16 out.txt",
    "tree -L 2 src",
    "rg -n 'hello.*' out.txt",
    "env LC_ALL=C cat out.txt",
    "nice -n 5 cat out.txt",
    "stdbuf -oL cat out.txt",
    "date -u +%Y-%m-%d",
]

MUTATES_OR_UNKNOWN = [
    # Runs a program: the whole point is that it does something.
    "pytest -q",
    "make test",
    "./run.sh",
    "cat\rscript",  # Bash names a different program; CR is not a word separator.
    "cargo test",
    "npm test",
    "python -c 'assert solve() == 42'",
    "git status",
    # Redirection and friends.
    "cat a.txt > b.txt",
    "cat a.txt >> b.txt",
    "cat a.txt | tee b.txt",
    "ls | xargs rm",
    "sudo cat /etc/shadow",
    "rm -rf /tmp/x",
    # Backgrounded: outlives the check, so it can do anything afterwards. The
    # mid-command form matters as much as the trailing one — `&` does not split a
    # segment, so an end-anchored test saw only `cat` here and passed the whole
    # thing. What is backgrounded is beside the point; escaping the check is not.
    "sleep 1 &",
    "cat a & rm -rf build",
    "cat a.txt & cat b.txt",
    # Name an output file as an *argument*, with no redirect to notice. This is why
    # sort/uniq/cut are not on the allowlist: admitting them let `sort -o merged.txt`
    # share a gather with a `cat merged.txt` reading the file it was still writing.
    "sort -o merged.txt a.txt b.txt",
    "uniq in.txt out.txt",
    "cut -f1 a.txt",
    "sort f.txt | uniq -c",
    # Command substitution runs an arbitrary inner command.
    "cat $(mkdir evil)",
    "cat `id`",
    # `cd` changes the working directory for everything after it in the same
    # command, which is exactly the ordering dependence concurrency breaks.
    "cd /app && cat x",
    # One mutating segment poisons the whole command.
    "cat a.txt && pytest",
    # Nothing at all.
    "",
    "   ",
]


@pytest.mark.parametrize("command", READ_ONLY)
def test_read_only_commands_are_side_effect_free(command):
    assert evidence.is_side_effect_free(command) is True


@pytest.mark.parametrize("command", MUTATES_OR_UNKNOWN)
def test_anything_else_fails_closed(command):
    assert evidence.is_side_effect_free(command) is False


@pytest.mark.parametrize("command", [
    "rg --pre=./preprocessor hello input.txt",
    "rg --hostname-bin ./preprocessor hello input.txt",
    "xxd -p -c8 input.txt output.txt",
    "xxd input.txt -p",
    "xxd -- input.txt output.txt",
    "xxd {input.txt,output.txt}",
    "tree -o output.txt .",
    "tree -afo output.txt .",
    "tree -R -L 1 -H .",
    "tree -aR -L 1 -H .",
    "LD_PRELOAD=./library.so cat input.txt",
    "env DYLD_INSERT_LIBRARIES=./library.dylib cat input.txt",
    "RIPGREP_CONFIG_PATH=./config rg hello input.txt",
    "PATH=. cat input.txt",
    "env FOO=1 cat input.txt",
    "time env LD_PRELOAD=./library.so cat input.txt",
    "./cat input.txt",
    "/tmp/cat input.txt",
    "./timeout 5 cat input.txt",
    "timeout 5 ./cat input.txt",
    "env -S 'cat input.txt'",
    "time -o output.txt cat input.txt",
    "timeout -s ABRT 5 cat input.txt",
    "cat <(touch output.txt)",
    "rg $RG_OPTIONS input.txt",
    "rg * input.txt",
    "rg {--pre=rm,input.txt}",
    "rg @(--pre=rm) input.txt",
    "printf -v PATH .",
    "printf '%n' PATH",
    "printf -- '%n' PATH",
    "file -C -m input.txt",
    "date 010100002025",
    "less -o output.txt input.txt",
    "cat 'unterminated",
])
async def test_readonly_refuses_argument_environment_and_executable_bypasses(command):
    allowed, _ = await PermissionEngine(mode="readonly").evaluate_tool_call(
        "bash", {"command": command}
    )
    assert not allowed


@pytest.mark.parametrize("command", [
    "xxd input.txt victim.txt",
    "rg --pre ./preprocessor hello input.txt",
    "rg --pr\\\ne ./preprocessor hello input.txt",
    'rg "--pr\\\ne" ./preprocessor hello input.txt',
    pytest.param("tree -R -L 1 -H .", marks=pytest.mark.skipif(
        shutil.which("tree") is None, reason="tree is not installed")),
])
async def test_readonly_shell_cannot_write_files_or_execute_preprocessors(tmp_path, command):
    (tmp_path / "input.txt").write_text("hello\n")
    victim = tmp_path / "victim.txt"
    victim.write_text("original\n")
    preprocessor = tmp_path / "preprocessor"
    preprocessor.write_text('#!/bin/sh\nprintf changed > victim.txt\ncat "$1"\n')
    preprocessor.chmod(0o700)
    (tmp_path / "child" / "grandchild").mkdir(parents=True)
    allowed, _ = await PermissionEngine(mode="readonly").evaluate_tool_call(
        "bash", {"command": command}
    )
    if allowed:
        await BashTool().execute(
            {"command": command}, LocalEnvironment(tmp_path), ToolContext(session_id="readonly")
        )
    assert victim.read_text() == "original\n"
    assert not list(tmp_path.rglob("00Tree.html"))
    assert not allowed


def test_quoted_operators_do_not_leak():
    """`split_segments` is quote-aware for a reason: a quote-blind check would read
    the body of `echo "..."` as though the shell were about to run it."""
    # Conservative either way, but for the right reason: the `>` inside the quotes
    # means this is treated as potentially writing rather than as a bare `echo`.
    assert evidence.is_side_effect_free('echo "cat a > b"') is False
    assert evidence.is_side_effect_free("echo hello") is True


def test_side_effect_free_set_excludes_every_assertion_runner():
    """The commands that prove the most are the ones that write. If one of these ever
    lands in the allowlist, two `pytest` runs could be gathered against one tree."""
    for runner in ("pytest", "make", "cargo", "npm", "tox", "mvn", "gradle", "ctest"):
        assert runner not in evidence.SIDE_EFFECT_FREE_COMMANDS


# -- how segments are formed ------------------------------------------------------


def test_segments_group_contiguous_readers():
    commands = ["cat a", "diff a b", "pytest", "ls", "wc -l f", "make"]
    segments = [(parallel, [c for _, c in group]) for parallel, group in
                _verification_segments(commands, enabled=True)]
    assert segments == [
        (True, ["cat a", "diff a b"]),
        (False, ["pytest"]),
        (True, ["ls", "wc -l f"]),
        (False, ["make"]),
    ]


def test_segments_preserve_original_indices():
    """checklist keys are `verify_cmd_<i>` in the agent's numbering; a segment walk
    must not renumber them."""
    segments = _verification_segments(["cat a", "pytest", "cat b", "cat c"], enabled=True)
    assert segments == [
        (False, [(0, "cat a")]),
        (False, [(1, "pytest")]),
        (True, [(2, "cat b"), (3, "cat c")]),
    ]
    assert group_command(segments[2][1], 3) == "cat c"


def test_lone_reader_is_a_serial_segment():
    """Nothing to overlap it with, and the serial path's interleaved screening cannot
    raise an approval prompt that would not otherwise have happened."""
    segments = _verification_segments(["cat a", "pytest"], enabled=True)
    assert [parallel for parallel, _ in segments] == [False, False]


def test_disabling_the_flag_restores_one_command_per_segment():
    segments = _verification_segments(["cat a", "diff a b", "ls"], enabled=False)
    assert segments == [(False, [(0, "cat a")]), (False, [(1, "diff a b")]), (False, [(2, "ls")])]
