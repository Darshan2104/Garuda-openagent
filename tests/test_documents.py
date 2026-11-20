import csv
from io import StringIO

import pytest

from garuda.tools.documents import ReadSpreadsheetTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment


async def test_csv_limits_complete_records_and_preserves_quoted_cells(tmp_path):
    (tmp_path / "sample.csv").write_text(
        'name,notes,label\r\nAlice,"first line\nsecond line, with comma","said ""hi"""\r\nBob,last,omitted\r\n',
        encoding="utf-8",
    )
    result = await ReadSpreadsheetTool().execute(
        {"path": "sample.csv", "max_rows": 2}, LocalEnvironment(tmp_path), ToolContext("docs"),
    )
    assert not result.is_error
    assert list(csv.reader(StringIO(result.content), delimiter="\t")) == [
        ["name", "notes", "label"],
        ["Alice", "first line\nsecond line, with comma", 'said "hi"'],
        ["...[truncated rows]"],
    ]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
async def test_spreadsheet_rejects_invalid_row_limits(tmp_path, limit):
    (tmp_path / "sample.csv").write_text("name\nAlice\n", encoding="utf-8")
    result = await ReadSpreadsheetTool().execute(
        {"path": "sample.csv", "max_rows": limit}, LocalEnvironment(tmp_path), ToolContext("docs"),
    )
    assert result.is_error and "max_rows" in result.content
