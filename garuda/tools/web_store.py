"""Bounded, process-local rendered web snapshots, isolated by session identity."""

import hashlib
import json
import threading
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass
class StoredPage:
    text: str
    source: dict[str, Any]
    size: int


class WebPageStore:
    def __init__(self, *, max_entries=64, max_bytes=16_000_000,
                 max_entry_bytes=5_000_000, session_entries=16, session_bytes=8_000_000) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.session_entries = session_entries
        self.session_bytes = session_bytes
        self._pages: OrderedDict[tuple[str, str], StoredPage] = OrderedDict()
        self._evicted: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, session: str, text: str, source: dict[str, Any]) -> dict[str, Any]:
        encoded = text.encode("utf-8")
        provenance = json.dumps(source, sort_keys=True, ensure_ascii=True).encode("utf-8")
        size = len(encoded) + len(provenance)
        if not session:
            return {"status": "unavailable", "reason": "missing session identity"}
        if size > min(self.max_entry_bytes, self.max_bytes, self.session_bytes):
            return {"status": "unavailable", "reason": "snapshot exceeds retention byte limit"}
        ref = "web:" + hashlib.sha256(encoded + b"\0" + provenance).hexdigest()
        key = (session, ref)
        with self._lock:
            self._pages[key] = StoredPage(text, deepcopy(source), size)
            self._pages.move_to_end(key)
            self._evicted.pop(key, None)
            evicted = []
            while True:
                own = [k for k in self._pages if k[0] == session]
                if len(own) > self.session_entries or sum(self._pages[k].size for k in own) > self.session_bytes:
                    victim = own[0]
                elif len(self._pages) > self.max_entries or sum(p.size for p in self._pages.values()) > self.max_bytes:
                    victim = next(iter(self._pages))
                else:
                    break
                del self._pages[victim]
                self._evicted[victim] = None
                self._evicted.move_to_end(victim)
                if victim[0] == session:
                    evicted.append(victim[1])
            while len(self._evicted) > self.max_entries * 2:
                self._evicted.popitem(last=False)
        return {"status": "retained", "ref": ref, "snapshot_bytes": size,
                "evicted_refs": evicted}

    def get(self, session: str, ref: str) -> tuple[str | None, StoredPage | None]:
        key = (session, ref)
        with self._lock:
            page = self._pages.get(key)
            if page is None:
                reason = "evicted" if key in self._evicted else "missing or expired"
                return (f"Web reference {reason} in this session; no refetch was attempted.", None)
            self._pages.move_to_end(key)
            return (None, StoredPage(page.text, deepcopy(page.source), page.size))
