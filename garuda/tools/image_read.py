import mimetypes
import shlex

from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

# Cap the encoded size we attach so a giant image can't blow the request.
MAX_IMAGE_B64_CHARS = 8_000_000  # ~6 MB raw


class ImageReadTool:
    name = "image_read"
    description = (
        "Load an image file from the workspace and attach it for you to view directly. "
        "The image is shown to you on your next step (requires a vision-capable model); "
        "state what you want to learn from it in `question`."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to an image file"},
            "question": {
                "type": "string",
                "description": "What you want to determine from the image (optional)",
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

        # Read bytes through the environment (portable across docker/remote).
        result = await env.execute(f"base64 < {shlex.quote(path)}", timeout=30.0)
        if result.exit_code != 0:
            return ToolResult(
                tool_call_id="", content=f"Image not found or unreadable: {path}", is_error=True
            )
        encoded = "".join(result.stdout.split())
        if not encoded:
            return ToolResult(tool_call_id="", content=f"Image is empty: {path}", is_error=True)
        if len(encoded) > MAX_IMAGE_B64_CHARS:
            return ToolResult(
                tool_call_id="",
                content=f"Image {path} is too large to attach ({len(encoded)} base64 chars).",
                is_error=True,
            )

        data_uri = f"data:{mime_type};base64,{encoded}"
        return ToolResult(
            tool_call_id="",
            content=(
                f"Loaded image {path} ({mime_type}). It is attached below for you to view. "
                f"Focus: {question}"
            ),
            images=[data_uri],
        )
