"""`evidence.is_side_effect_free`: which verification commands may run concurrently.

A different axis from the discriminating-power classification in the same module.
This one gates concurrency, so it is an allowlist and it fails closed: being wrong
means two commands racing at the completion gate, while being conservative only
means they run one after another as they always did.
"""

import pytest

from garuda.core import evidence
from garuda.core.verifier import _verification_segments, group_command

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
    "FOO=1 timeout 5 cat x",
    "/usr/bin/cat x",
]

MUTATES_OR_UNKNOWN = [
    # Runs a program: the whole point is that it does something.
    "pytest -q",
    "make test",
    "./run.sh",
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
