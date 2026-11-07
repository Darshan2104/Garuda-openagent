"""A small deterministic stdio language server for tests (run with sys.executable).

Understands just enough Python-ish text to answer real questions: ``def NAME``
and ``class NAME`` lines declare symbols, every whole-word occurrence is a
reference, and a line containing ``ERROR`` gets an error diagnostic at that
word. Every position it sends or reads is UTF-16, computed from the text.

Modes (``--mode``): ``normal``; ``slow`` (definition never answers until
cancelled); ``crash`` (exits when asked for a definition); ``hang-init`` (never
answers initialize); ``no-hover`` (declares no hover capability); ``stale``
(publishes diagnostics only for the first version of a document);
``unversioned`` (publishes diagnostics without a version); ``utf8`` (claims
UTF-8 positions). ``--log PATH`` appends every received message as JSON lines.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--mode", default="normal")
parser.add_argument("--log")
opts = parser.parse_args()

stdin = sys.stdin.buffer
stdout = sys.stdout.buffer
docs: dict[str, str] = {}
first_version: dict[str, int] = {}
next_id = 1000
root = None
versions = {}


def log(message) -> None:
    if opts.log:
        with open(opts.log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(message) + "\n")


def send(message) -> None:
    body = json.dumps(message).encode("utf-8")
    stdout.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    stdout.flush()


def read():
    length = None
    while True:
        line = stdin.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode().partition(":")
        if name.lower() == "content-length":
            length = int(value)
    return json.loads(stdin.read(length))


def lines_of(text: str) -> list[str]:
    return re.split(r"\r\n|\r|\n", text)


def u16(text: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def from_u16(line: str, offset: int) -> int:
    units = 0
    for i, c in enumerate(line):
        if units >= offset:
            return i
        units += 2 if ord(c) > 0xFFFF else 1
    return len(line)


def span(line_no: int, line: str, start: int, length: int) -> dict:
    a = u16(line[:start])
    return {"start": {"line": line_no, "character": a},
            "end": {"line": line_no, "character": a + u16(line[start:start + length])}}


def word_at(uri: str, pos: dict) -> str | None:
    lines = lines_of(docs.get(uri, ""))
    if pos["line"] >= len(lines):
        return None
    line = lines[pos["line"]]
    index = from_u16(line, pos["character"])
    for match in re.finditer(r"\w+", line):
        if match.start() <= index < match.end():
            return match.group()
    return None


def own_uri(uri: str) -> str:
    # Re-encode the way this server would, not echo the client's spelling.
    path = urllib.parse.unquote(urllib.parse.urlsplit(uri).path)
    return "file://" + urllib.parse.quote(path)


def occurrences(word: str):
    for uri, text in docs.items():
        for n, line in enumerate(lines_of(text)):
            for match in re.finditer(rf"\b{re.escape(word)}\b", line):
                declares = re.search(rf"\b(def|class)\s+{re.escape(word)}\b", line) is not None
                yield uri, n, line, match.start(), declares


def publish(uri: str, version: int) -> None:
    if opts.mode == "stale" and first_version.get(uri, version) != version:
        return
    diags = []
    for n, line in enumerate(lines_of(docs[uri])):
        index = line.find("ERROR")
        if index >= 0:
            diags.append({"range": span(n, line, index, 5), "severity": 1,
                          "source": "fake", "code": "E1", "message": f"bad thing on line {n + 1}"})
    params = {"uri": uri, "diagnostics": diags}
    if opts.mode != "unversioned":
        params["version"] = version
    send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": params})


def symbols(uri: str) -> list:
    out, current = [], None
    for n, line in enumerate(lines_of(docs.get(uri, ""))):
        match = re.match(r"(\s*)(class|def)\s+(\w+)", line)
        if not match:
            continue
        rng = span(n, line, match.start(3), len(match.group(3)))
        symbol = {"name": match.group(3), "kind": 5 if match.group(2) == "class" else 12,
                  "range": rng, "selectionRange": rng, "children": []}
        if match.group(1) and current is not None:
            symbol["kind"] = 6
            current["children"].append(symbol)
        else:
            out.append(symbol)
            current = symbol if match.group(2) == "class" else None
    return out


def handle(message) -> None:
    global next_id, root
    method, params, mid = message.get("method"), message.get("params") or {}, message.get("id")
    if method == "initialize":
        if opts.mode == "hang-init":
            return
        root = Path(urllib.parse.unquote(urllib.parse.urlsplit(params["rootUri"]).path))
        caps = {"textDocumentSync": 1, "definitionProvider": True, "referencesProvider": True,
                "hoverProvider": opts.mode != "no-hover", "documentSymbolProvider": True,
                "renameProvider": True, "codeActionProvider": True}
        if opts.mode == "empty-options":
            caps.update(renameProvider={}, codeActionProvider={})
        if opts.mode == "utf8":
            caps["positionEncoding"] = "utf-8"
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": caps}})
    elif method == "initialized":
        # Exercise the client's answers to server-to-client requests.
        send({"jsonrpc": "2.0", "id": next_id, "method": "workspace/configuration",
              "params": {"items": [{"section": "python"}, {"section": "x"}]}})
        send({"jsonrpc": "2.0", "id": next_id + 1, "method": "workspace/applyEdit",
              "params": {"edit": {"changes": {}}}})
        send({"jsonrpc": "2.0", "id": next_id + 2, "method": "custom/doSomething", "params": {}})
        next_id += 3
    elif method == "textDocument/didOpen":
        doc = params["textDocument"]
        docs[doc["uri"]] = doc["text"]
        first_version[doc["uri"]] = doc["version"]
        versions[doc["uri"]] = doc["version"]
        publish(doc["uri"], doc["version"])
    elif method == "textDocument/didChange":
        doc = params["textDocument"]
        docs[doc["uri"]] = params["contentChanges"][-1]["text"]
        versions[doc["uri"]] = doc["version"]
        publish(doc["uri"], doc["version"])
    elif method == "textDocument/definition":
        if opts.mode == "crash":
            sys.stderr.write("fake server: crashing on purpose\n")
            sys.stderr.flush()
            os._exit(3)
        if opts.mode == "slow":
            return  # answered only by $/cancelRequest
        word = word_at(params["textDocument"]["uri"], params["position"])
        result = [{"uri": own_uri(uri), "range": span(n, line, start, len(word))}
                  for uri, n, line, start, declares in occurrences(word or "") if declares]
        send({"jsonrpc": "2.0", "id": mid, "result": result})
    elif method in ("textDocument/rename", "textDocument/codeAction"):
        if opts.mode == "slow-rename":
            return
        if method == "textDocument/rename":
            position = params["position"]
            new_name = params["newName"]
        else:
            position = params["range"]["start"]
            new_name = "renamed"
        word = word_at(params["textDocument"]["uri"], position)
        # Include unopened on-disk references, like an indexed server would.
        for path in sorted(root.glob("*.py")):
            docs.setdefault(path.as_uri(), path.read_bytes().decode("utf-8"))
        changes = {}
        for uri, n, line, start, _ in occurrences(word or ""):
            changes.setdefault(uri, []).append(
                {"range": span(n, line, start, len(word)), "newText": new_name})
        if opts.mode == "outside-edit":
            changes[(root.parent / "outside.py").as_uri()] = next(iter(changes.values()))
        if opts.mode == "alias-edit":
            changes[(root / "alias.py").as_uri()] = next(iter(changes.values()))
        if opts.mode == "overlap-edit":
            next(iter(changes.values())).append(next(iter(changes.values()))[0])
        if opts.mode == "bad-range":
            next(iter(changes.values()))[0]["range"]["start"]["character"] = 6
        edit = {"documentChanges": [
            {"textDocument": {"uri": own_uri(uri), "version":
                              (versions.get(uri, 1) - 1 if opts.mode == "stale-edit"
                               else versions.get(uri))}, "edits": edits}
            for uri, edits in changes.items()]}
        if opts.mode == "multiline-edit":
            edit["documentChanges"][0]["edits"] = [{
                "range": {"start": {"line": 0, "character": 0},
                          "end": {"line": 1, "character": 0}}, "newText": "prefix\n"}]
        if opts.mode == "changes-edit":
            edit = {"changes": changes}
        if opts.mode in ("create-edit", "rename-edit", "delete-edit"):
            edit["documentChanges"].append({"kind": opts.mode.split("-")[0],
                                           "uri": (root / "new.py").as_uri()})
        if method == "textDocument/codeAction":
            result = [{"title": "Rename symbol", "edit": edit}]
            if opts.mode == "command-action":
                result[0]["command"] = {"command": "dangerous", "arguments": []}
            elif opts.mode == "unresolved-action":
                result[0].pop("edit")
                result[0]["data"] = {"id": "resolve-me"}
            elif opts.mode == "disabled-action":
                result[0]["disabled"] = {"reason": "unavailable"}
            elif opts.mode == "duplicate-action":
                result.append(result[0])
        else:
            result = edit
        send({"jsonrpc": "2.0", "id": mid, "result": result})
    elif method == "textDocument/references":
        word = word_at(params["textDocument"]["uri"], params["position"])
        include = params.get("context", {}).get("includeDeclaration", True)
        result = [{"uri": own_uri(uri), "range": span(n, line, start, len(word))}
                  for uri, n, line, start, declares in occurrences(word or "")
                  if include or not declares]
        send({"jsonrpc": "2.0", "id": mid, "result": result})
    elif method == "textDocument/hover":
        word = word_at(params["textDocument"]["uri"], params["position"])
        result = {"contents": {"kind": "markdown", "value": f"symbol `{word}`"}} if word else None
        send({"jsonrpc": "2.0", "id": mid, "result": result})
    elif method == "textDocument/documentSymbol":
        send({"jsonrpc": "2.0", "id": mid, "result": symbols(params["textDocument"]["uri"])})
    elif method == "$/cancelRequest":
        send({"jsonrpc": "2.0", "id": params["id"],
              "error": {"code": -32800, "message": "cancelled"}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "exit":
        sys.exit(0)
    elif mid is not None and method is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": method}})


while True:
    msg = read()
    if msg is None:
        break
    log(msg)
    handle(msg)
