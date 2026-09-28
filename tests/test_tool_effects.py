from garuda.tools import builtin_registry
from garuda.tools.discovery import SearchToolTool, UseToolTool
from garuda.tools.protocol import ToolEffect, tool_effect

EXPECTED_BUILTIN_EFFECTS = {
    "bash": ToolEffect.MUTATING,
    "bash_background": ToolEffect.MUTATING,
    "task_output": ToolEffect.READ_ONLY,
    "kill_task": ToolEffect.MUTATING,
    "read_file": ToolEffect.READ_ONLY,
    "write_file": ToolEffect.MUTATING,
    "edit": ToolEffect.MUTATING,
    "multi_edit": ToolEffect.MUTATING,
    "grep": ToolEffect.READ_ONLY,
    "glob": ToolEffect.READ_ONLY,
    "ls": ToolEffect.READ_ONLY,
    "todo": ToolEffect.MUTATING,
    "update_goal": ToolEffect.MUTATING,
    "contract": ToolEffect.MUTATING,
    "web_fetch": ToolEffect.EXTERNAL_READ,
    "web_search": ToolEffect.EXTERNAL_READ,
    "task_complete": ToolEffect.MUTATING,
    "tmux_exec": ToolEffect.MUTATING,
    "tmux_capture": ToolEffect.READ_ONLY,
    "image_read": ToolEffect.READ_ONLY,
    "read_pdf": ToolEffect.READ_ONLY,
    "read_spreadsheet": ToolEffect.READ_ONLY,
    "invoke_subagent": ToolEffect.EXTERNAL_SIDE_EFFECT,
    "buffer_grep": ToolEffect.READ_ONLY,
    "buffer_slice": ToolEffect.READ_ONLY,
    "buffer_list": ToolEffect.READ_ONLY,
    "buffer_query": ToolEffect.READ_ONLY,
}


def test_every_builtin_has_an_intentional_effect_classification():
    tools = {tool.name: tool for tool in builtin_registry().all_tools()}
    # Set equality is deliberate: deleting a built-in from the expected map or
    # adding one without deciding its effect must fail this test.
    assert set(tools) == set(EXPECTED_BUILTIN_EFFECTS)
    assert {name: tool_effect(tool) for name, tool in tools.items()} == (
        EXPECTED_BUILTIN_EFFECTS
    )
    assert ToolEffect.UNKNOWN not in EXPECTED_BUILTIN_EFFECTS.values()


def test_missing_or_invalid_effect_fails_closed():
    class Missing:
        pass

    class Invalid:
        effect = "definitely-safe"

    assert tool_effect(Missing()) is ToolEffect.UNKNOWN
    assert tool_effect(Invalid()) is ToolEffect.UNKNOWN


def test_lazy_meta_tools_are_classified_by_their_actual_boundary():
    assert tool_effect(SearchToolTool({})) is ToolEffect.READ_ONLY
    assert tool_effect(UseToolTool({})) is ToolEffect.UNKNOWN
