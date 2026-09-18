"""Grounding a conversation in documents and web pages.

The mechanism is deliberately dull: **a source becomes a file in the agent's workspace, and
the next turn's task names it.** Nothing here injects text into the model's context.

That choice is the whole design. The agent already has ``read_file``, ``read_pdf`` and
``read_spreadsheet``, and a source it reads with its own tools is one it can re-read, quote a
specific line from, and grep — while a blob pasted into the prompt is a thing it must carry in
context for the rest of the conversation whether or not any later question needs it. Pasting
also silently caps the source at whatever fits, which is the failure mode where an answer is
confidently wrong about page 40 of a PDF.

Two safety properties:

* **A URL is fetched through the same SSRF-vetted fetcher the ``web_fetch`` tool uses.** Not a
  second fetcher written for this module: ``tools/web.py`` resolves the host, refuses private
  and link-local addresses, and pins the connection to the vetted IP so a DNS rebind between
  check and connect cannot land elsewhere. A dashboard endpoint that fetched URLs by any other
  path would be a proxy into everything the agent's own tool is forbidden from reaching.
* **The filename is rebuilt, never echoed.** ``name`` arrives from a browser, so a single
  component is derived from it by keeping only ``[A-Za-z0-9._-]``; the result cannot contain a
  separator, so no traversal is expressible regardless of what was sent.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Where sources land, relative to the workspace root.
#:
#: Not dot-prefixed, deliberately. The agent finds its own way around with ``ls`` and ``glob``,
#: and a hidden directory is one it can only read if the prompt happens to name every file in
#: it. Visible costs a directory in the working tree and buys discoverability.
GROUNDING_DIR = "grounding"

#: Decoded size ceiling for one uploaded file. The HTTP layer caps the request body; this caps
#: what a base64 payload is allowed to expand into, which is the number that matters.
MAX_SOURCE_BYTES = 8 * 1024 * 1024

#: How much text one fetched page is kept as. A page is being saved for an agent to read, not
#: archived, and ``web_fetch``'s own default is smaller still.
MAX_URL_TEXT_BYTES = 512 * 1024

#: How long a fetch may take before the route gives up. Slightly under the route's own budget
#: so the timeout is reported as a fetch failure naming the URL, not as a generic 504.
FETCH_TIMEOUT_SECONDS = 60.0

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MULTI_DOT = re.compile(r"\.{2,}")

#: Which tool reads which extension. Only what the toolkit actually provides — naming a tool
#: that is not in the profile would send the agent looking for something that does not exist.
_READERS = {
    ".pdf": "read_pdf",
    ".xlsx": "read_spreadsheet",
    ".xls": "read_spreadsheet",
    ".csv": "read_spreadsheet",
}


class SourceError(RuntimeError):
    """The source could not be obtained. Surfaces as a 502 naming what failed."""


@dataclass(frozen=True)
class Source:
    """One grounded document, as the agent will see it."""

    kind: str
    #: Workspace-relative, and the only string the prompt shows the agent.
    path: str
    bytes: int
    #: The URL a fetched page came from. ``None`` for an upload — and worth keeping, because
    #: "where did this claim come from" is unanswerable from a slugged filename alone.
    origin: str | None = None
    #: Which tool to read it with, when it is not just ``read_file``.
    reader: str | None = None
    added_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def safe_name(raw: Any, *, fallback: str = "source") -> str:
    """A single filename component derived from untrusted input.

    Rebuilt rather than validated: every run of unacceptable characters collapses to ``_``, so
    there is no input for which this returns something containing a separator. ``..`` is
    flattened too — harmless once separators are gone, but a file literally named ``..`` is
    unopenable, and failing at write time is a worse error than not creating it.
    """
    name = str(raw or "").strip()
    # A browser sending a full path in `name` is not an attack, it is what `<input type=file>`
    # does on some platforms. Take the last component and rebuild that.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    return _slug(name, fallback=fallback)


def _slug(raw: str, *, fallback: str) -> str:
    """The rebuild half of ``safe_name``, without taking the last path component.

    Separate because a URL wants its whole shape flattened into one name —
    ``example.com/docs/guide`` should read as ``example.com_docs_guide``, where taking the last
    component would name every page in a site ``guide``, ``index`` or nothing at all.
    """
    name = _UNSAFE.sub("_", raw).strip("._-")
    name = _MULTI_DOT.sub(".", name)
    if not name:
        return fallback
    # Leave room for the collision suffix and the extension without hitting a filesystem's
    # 255-byte component limit.
    return name[:120]


def url_filename(url: str) -> str:
    """A readable filename for a fetched page: host, then path, then ``.md``."""
    parsed = urllib.parse.urlparse(url)
    stem = _slug(f"{parsed.netloc}{parsed.path}".strip("/"), fallback="page")
    # `.md` because the body is extracted text, not the HTML that was served. Calling it
    # `.html` would invite the agent to expect markup that is no longer there.
    return f"{stem}.md" if not stem.endswith(".md") else stem


def read_hint(path: str) -> str | None:
    return _READERS.get(Path(path).suffix.lower())


def _unique_path(directory: Path, name: str) -> Path:
    """``name`` inside ``directory``, suffixed if something is already there.

    Suffixing rather than overwriting. Re-uploading under a name already used usually means a
    corrected version — but the agent may already have read the old one and be holding its
    contents in context, so silently swapping the bytes under a path it has quoted would make
    its citations wrong with no trace. Two files and two names is the honest outcome.
    """
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    for index in range(2, 100):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise SourceError(f"Too many files already named like {name!r} in {GROUNDING_DIR}/.")


def decode_upload(raw: Any) -> bytes:
    """The bytes of an uploaded file, from base64.

    base64 in a JSON body rather than ``multipart/form-data``: a multipart parser is a
    hand-written state machine over attacker-controlled bytes, and this needs to accept
    exactly one file. ``binascii`` is the parser instead.
    """
    if not isinstance(raw, str) or not raw.strip():
        # Covers the empty file too: base64 of no bytes is the empty string, so there is no
        # separate "decoded to nothing" case to check for below.
        raise SourceError("`content_b64` is required and must be non-empty base64.")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise SourceError("`content_b64` is not valid base64.") from None
    if len(data) > MAX_SOURCE_BYTES:
        raise SourceError(
            f"That file decodes to {len(data)} bytes, over the {MAX_SOURCE_BYTES}-byte limit."
        )
    return data


async def store_file(workspace: Path, name: Any, content_b64: Any) -> Source:
    """Write an uploaded file into the workspace's grounding directory."""
    data = decode_upload(content_b64)
    target = await asyncio.to_thread(_write_bytes, workspace, safe_name(name), data)
    relative = f"{GROUNDING_DIR}/{target.name}"
    return Source(
        kind="file",
        path=relative,
        bytes=len(data),
        reader=read_hint(relative),
        added_at=time.time(),
    )


async def store_url(workspace: Path, url: Any) -> Source:
    """Fetch a page and save its extracted text into the grounding directory.

    Goes through ``tools/web.py``'s own fetcher — private, and used anyway, because it is the
    one that pins the connection to a vetted address. A public wrapper would be a nicer import
    and exactly the same code; a fresh ``urlopen`` here would be a second network path with
    none of the vetting, which is the thing worth avoiding.
    """
    from garuda.tools.web import _blocking_fetch, validate_http_url

    if not isinstance(url, str) or not url.strip():
        raise SourceError("`url` is required and must be an http(s) URL.")
    url = url.strip()
    complaint = validate_http_url(url)
    if complaint:
        raise SourceError(complaint)
    try:
        error, text = await asyncio.wait_for(
            asyncio.to_thread(_blocking_fetch, url, MAX_URL_TEXT_BYTES),
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise SourceError(f"Timed out fetching {url}.") from None
    if error:
        raise SourceError(error)
    if not text.strip():
        raise SourceError(f"{url} returned no readable text.")
    body = f"<!-- fetched from {url} -->\n\n{text}"
    data = body.encode("utf-8")
    target = await asyncio.to_thread(_write_bytes, workspace, url_filename(url), data)
    relative = f"{GROUNDING_DIR}/{target.name}"
    return Source(
        kind="url",
        path=relative,
        bytes=len(data),
        origin=url,
        reader=None,
        added_at=time.time(),
    )


def _write_bytes(workspace: Path, name: str, data: bytes) -> Path:
    """Blocking half of both writers, so the loop never waits on a disk.

    Writes to the **host** path rather than through ``Environment.write_file``, for two
    reasons. The protocol's ``write_file`` takes ``str``, and a PDF is not text — routing bytes
    through it would corrupt exactly the documents this feature exists for. And every workspace
    kind the dashboard offers either *is* this directory (``local``) or bind-mounts it
    (``docker`` mounts it at ``/workspace``), so the file is visible to the agent either way.
    """
    directory = workspace / GROUNDING_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = _unique_path(directory, name)
    try:
        target.write_bytes(data)
    except OSError as exc:
        raise SourceError(f"Could not write {target}: {exc}") from None
    logger.info("Grounded %s (%d bytes)", target, len(data))
    return target


def announce(sources: list[Source]) -> str:
    """The preamble prepended to the next turn's task.

    Phrased as an instruction rather than a note. A list of paths with no verb gets ignored
    about as often as it gets read, and the whole point of adding a source is that the answer
    is grounded in it.
    """
    if not sources:
        return ""
    lines = []
    for source in sources:
        parts = [f"- {source.path} ({_size(source.bytes)})"]
        if source.origin:
            parts.append(f"fetched from {source.origin}")
        if source.reader:
            parts.append(f"read it with `{source.reader}`")
        lines.append(" — ".join(parts))
    plural = "these files" if len(sources) > 1 else "this file"
    return (
        f"I have added {plural} to your workspace as source material. Read "
        f"{'them' if len(sources) > 1 else 'it'} before answering, and ground your answer in "
        f"{'their' if len(sources) > 1 else 'its'} contents rather than on prior knowledge:\n"
        + "\n".join(lines)
        + "\n\n"
    )


def _size(count: int) -> str:
    if count >= 1024 * 1024:
        return f"{count / (1024 * 1024):.1f} MB"
    if count >= 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count} B"
