import base64
import mimetypes
from pathlib import Path

import litellm

from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


class ImageReadTool:
    name = "image_read"
    description = (
        "Read an image file from the workspace. Uses the configured model for vision analysis."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to an image file"},
            "question": {
                "type": "string",
                "description": "What to analyze in the image",
                "default": "Describe this image in detail.",
            },
        },
        "required": ["path"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        path = arguments["path"]
        question = arguments.get("question", "Describe this image in detail.")
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = Path(env.workspace_root) / file_path
        if not file_path.exists():
            return ToolResult(tool_call_id="", content=f"Image not found: {path}", is_error=True)

        mime_type, _ = mimetypes.guess_type(str(file_path))
        if not mime_type or not mime_type.startswith("image/"):
            return ToolResult(tool_call_id="", content=f"Not an image file: {path}", is_error=True)

        encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
        if ctx.model is None:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Image loaded ({mime_type}, {len(encoded)} base64 chars). "
                    "No model attached for vision analysis."
                ),
            )

        response = await litellm.acompletion(
            model=ctx.model.model_name,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                        },
                    ],
                }
            ],
        )
        content = response.choices[0].message.content or "No analysis returned."
        return ToolResult(tool_call_id="", content=content)
