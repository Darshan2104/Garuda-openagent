import mimetypes
import shlex

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

        mime_type, _ = mimetypes.guess_type(path)
        if not mime_type or not mime_type.startswith("image/"):
            return ToolResult(tool_call_id="", content=f"Not an image file: {path}", is_error=True)

        # Read the bytes through the environment (base64), so it works for docker/
        # remote workspaces (where workspace_root is a container path) and reaches
        # the same files as bash — instead of reading the harness host filesystem.
        # `base64 < file` reads stdin, which is portable (macOS base64 rejects a
        # positional filename; GNU accepts both).
        result = await env.execute(f"base64 < {shlex.quote(path)}", timeout=30.0)
        if result.exit_code != 0:
            return ToolResult(
                tool_call_id="", content=f"Image not found or unreadable: {path}", is_error=True
            )
        encoded = "".join(result.stdout.split())
        if not encoded:
            return ToolResult(tool_call_id="", content=f"Image is empty: {path}", is_error=True)
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
