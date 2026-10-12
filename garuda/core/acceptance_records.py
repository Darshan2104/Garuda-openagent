"""Strict private check intent and immutable receipt transitions.

Only the bounded owned acceptance worker calls this synchronous store. A lost
publication response cannot roll a transition back or authorize another launch.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import stat
import time
import uuid
from pathlib import Path

from garuda.runtime.private_records import FLAGS, LIMIT, open_directory, private_file, read_record
from garuda.runtime.strict_store import CorruptRecord, StorageUnavailable, write_locked_document

BINDING = {"parent_id", "phase_id", "check_id", "check_authority", "attempt_id", "workspace", "owner",
           "workspace_identity", "candidate", "evidence_digest", "authority_digest", "ceiling_digest", "request_digest"}


def digest(document) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def validate_binding(binding: dict) -> None:
    if not isinstance(binding, dict) or set(binding) != BINDING:
        raise CorruptRecord("invalid check binding schema")
    if binding["check_authority"] not in {"user-config", "trusted-project", "user-request"}:
        raise CorruptRecord("invalid check authority binding")
    for key in ("parent_id", "phase_id", "attempt_id"):
        if str(uuid.UUID(binding[key])) != binding[key]:
            raise CorruptRecord("noncanonical check binding id")
    for key in ("check_id", "evidence_digest", "authority_digest", "ceiling_digest", "request_digest"):
        value = binding[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise CorruptRecord("invalid binding digest")
    if (not isinstance(binding["candidate"], str) or len(binding["candidate"]) != 16
            or any(c not in "0123456789abcdef" for c in binding["candidate"])):
        raise CorruptRecord("invalid candidate fingerprint")
    if not isinstance(binding["workspace"], str) or not os.path.isabs(binding["workspace"]):
        raise CorruptRecord("invalid workspace binding")
    identity = binding["workspace_identity"]
    if (not isinstance(identity, dict) or set(identity) != {"device", "inode"}
            or any(type(v) is not int or v < 0 for v in identity.values())):
        raise CorruptRecord("invalid workspace directory identity")
    owner = binding["owner"]
    if (not isinstance(owner, dict) or set(owner) != {"pid", "identity", "pgid", "epoch"}
            or any(type(owner[k]) is not int or owner[k] <= 0 for k in ("pid", "pgid"))
            or any(not isinstance(owner[k], str) or not owner[k] or owner[k] == "unknown"
                   for k in ("identity", "epoch"))):
        raise CorruptRecord("invalid retained owner binding")


class CheckRecords:
    def __init__(self, session_dir: str, binding: dict):
        validate_binding(binding)
        self.parent = Path(session_dir)
        if self.parent.name != binding["parent_id"]:
            raise StorageUnavailable("check store does not match its parent")
        self.path = self.parent / f"acceptance-{binding['phase_id']}"
        self.policy_name = f"acceptance-policy-{binding['phase_id']}.json"
        self.binding = binding

    @contextlib.contextmanager
    def locked(self, *, create=False):
        parent = open_directory(self.parent)
        directory = lock = None
        try:
            if os.fstat(parent).st_uid != os.getuid():
                raise StorageUnavailable("parent session has a foreign owner")
            marker = read_record(parent, self.policy_name)
            created = False
            if create and marker is None:
                try:
                    os.mkdir(self.path.name, mode=0o700, dir_fd=parent)
                    created = True
                except FileExistsError:
                    pass
            directory = os.open(self.path.name, os.O_RDONLY | os.O_DIRECTORY | FLAGS, dir_fd=parent)
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise StorageUnavailable("check directory is not private")
            if created:
                lock = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | FLAGS, 0o600, dir_fd=directory)
            else:
                lock = os.open(".lock", os.O_RDWR | FLAGS, dir_fd=directory)
            private_file(lock)
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StorageUnavailable("check store lock timed out") from None
                    time.sleep(0.01)
            # Re-read under the check directory lock: a concurrent initializer
            # may have published the policy after our first observation.
            marker = read_record(parent, self.policy_name)
            wanted = {"version": 1, "binding": self.phase_binding,
                      "directory": {"device": info.st_dev, "inode": info.st_ino}}
            initialized = marker is None
            if initialized:
                if not created:
                    raise CorruptRecord("check directory has no intact policy marker")
                write_locked_document(parent, self.parent / self.policy_name, wanted)
            elif digest(marker) != digest(wanted):
                raise CorruptRecord("check policy or directory identity differs")
            yield directory, initialized
        except OSError as exc:
            raise StorageUnavailable("check storage is unavailable; preserve existing records") from exc
        finally:
            for fd in (lock, directory, parent):
                if fd is not None:
                    os.close(fd)

    @property
    def phase_binding(self):
        return {k: v for k, v in self.binding.items() if k not in {"check_id", "check_authority", "attempt_id"}}

    def _phase(self, directory):
        phase = read_record(directory, "request.json")
        if (phase is None or set(phase) != {"version", "binding", "attempts"}
                or digest(phase["binding"]) != digest(self.phase_binding) or not isinstance(phase["attempts"], list)
                or any(not isinstance(v, str) for v in phase["attempts"])
                or len(phase["attempts"]) > 64 or len(set(phase["attempts"])) != len(phase["attempts"])):
            raise CorruptRecord("verification phase registry is missing or corrupt")
        for value in phase["attempts"]:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise CorruptRecord("invalid reserved check attempt")
        return phase

    def _read_intent(self, directory):
        intent = read_record(directory, self.intent_name)
        if intent is not None and (set(intent) != {
                "version", "binding", "phase", "summary", "limits", "supervisor", "command", "receipt_digest"}
                or digest(intent["binding"]) != digest(self.binding) or intent["phase"] not in {
                    "prepared", "launch-authorized", "active", "terminal"}):
            raise CorruptRecord("check intent binding or state differs")
        if intent is not None and self.binding["attempt_id"] not in self._phase(directory)["attempts"]:
            raise CorruptRecord("check intent is outside its phase registry")
        if intent is not None:
            self._limits(intent["limits"])
            if not isinstance(intent["summary"], str) or len(intent["summary"]) > 512:
                raise CorruptRecord("invalid durable check summary")
            phase = intent["phase"]
            if phase == "prepared" and any(intent[k] is not None for k in ("supervisor", "command", "receipt_digest")):
                raise CorruptRecord("prepared intent has contradictory activation evidence")
            if phase != "prepared":
                self._identity(intent["supervisor"])
            if phase == "active":
                self._identity(intent["command"])
            if phase == "launch-authorized" and intent["command"] is not None:
                raise CorruptRecord("authorized intent has unpublished activation")
            if phase != "terminal" and intent["receipt_digest"] is not None:
                raise CorruptRecord("nonterminal intent has a terminal commitment")
            if phase == "terminal" and (not isinstance(intent["receipt_digest"], str)
                    or len(intent["receipt_digest"]) != 64
                    or any(c not in "0123456789abcdef" for c in intent["receipt_digest"])):
                raise CorruptRecord("terminal intent has no receipt commitment")
        return intent

    @staticmethod
    def _identity(identity):
        if (not isinstance(identity, dict) or set(identity) != {"pid", "identity", "pgid"}
                or any(type(identity[k]) is not int or identity[k] <= 1 for k in ("pid", "pgid"))
                or not isinstance(identity["identity"], str) or not identity["identity"]
                or identity["identity"] == "unknown"):
            raise CorruptRecord("invalid supervised process identity")

    @property
    def intent_name(self):
        return f"intent-{self.binding['attempt_id']}.json"

    @property
    def receipt_name(self):
        return f"receipt-{self.binding['attempt_id']}.json"

    def _write(self, directory, name, document, *, immutable=False):
        raw = json.dumps(document, sort_keys=True).encode()
        if len(raw) > LIMIT:
            raise StorageUnavailable("check publication exceeds its bound")
        previous = read_record(directory, name)
        if immutable and previous is not None:
            if digest(previous) != digest(document):
                raise CorruptRecord("immutable check publication differs")
            return
        write_locked_document(directory, self.path / name, document)

    @staticmethod
    def _limits(limits):
        maxima = {"timeout": 900, "phase_timeout": 3600, "term_grace": 3, "kill_grace": 3}
        if (not isinstance(limits, dict) or set(limits) != set(maxima)
                or any(type(limits[k]) not in (int, float) or not math.isfinite(limits[k])
                       or not 0 < limits[k] <= maximum for k, maximum in maxima.items())):
            raise CorruptRecord("invalid durable check limits")

    def prepare(self, summary: str, limits: dict):
        if not isinstance(summary, str) or len(summary) > 512:
            raise CorruptRecord("invalid check summary")
        self._limits(limits)
        # Only a newly initialized policy may establish the phase registry.
        # Existing directories with lost records are uncertainty, not fresh work.
        with self.locked(create=True) as (directory, initialized):
            phase = read_record(directory, "request.json")
            if phase is None and initialized:
                self._write(directory, "request.json", {"version": 1, "binding": self.phase_binding,
                                                         "attempts": []}, immutable=True)
            phase = self._phase(directory)
            previous = self._read_intent(directory)
            if previous is not None:
                return {"created": False, "phase": previous["phase"]}
            if self.binding["attempt_id"] in phase["attempts"]:
                raise CorruptRecord("reserved check intent is missing; launch is uncertain")
            if read_record(directory, self.receipt_name) is not None:
                raise CorruptRecord("receipt has no matching intent")
            if len(phase["attempts"]) >= 64:
                raise StorageUnavailable("verification phase attempt bound reached")
            self._write(directory, "request.json", {**phase, "attempts": [
                *phase["attempts"], self.binding["attempt_id"]]})
            intent = {"version": 1, "binding": self.binding, "phase": "prepared",
                      "summary": summary, "limits": limits, "supervisor": None, "command": None,
                      "receipt_digest": None}
            self._write(directory, self.intent_name, intent, immutable=True)
            return {"created": True, "phase": "prepared"}

    def transition(self, expected: str, phase: str, **fields):
        if (expected, phase) not in {("prepared", "launch-authorized"), ("launch-authorized", "active")}:
            raise CorruptRecord("unsupported check transition")
        if set(fields) != ({"supervisor"} if phase == "launch-authorized" else {"command"}):
            raise CorruptRecord("invalid transition fields")
        self._identity(next(iter(fields.values())))
        with self.locked() as (directory, _):
            self._phase(directory)
            intent = self._read_intent(directory)
            if intent is None or intent["phase"] != expected:
                raise CorruptRecord("check was already authorized or differs")
            intent = {**intent, "phase": phase, **fields}
            self._write(directory, self.intent_name, intent)
            return {"phase": phase, "digest": digest(intent)}

    def terminal(self, receipt: dict):
        with self.locked() as (directory, _):
            self._phase(directory)
            intent = self._read_intent(directory)
            if intent is None or intent["phase"] not in {"launch-authorized", "active", "terminal"}:
                raise CorruptRecord("receipt has no launch authorization")
            self._validate_receipt(intent, receipt)
            self._write(directory, self.receipt_name, receipt, immutable=True)
            self._write(directory, self.intent_name, {**intent, "phase": "terminal", "receipt_digest": digest(receipt)})
            return {"receipt_digest": digest(receipt)}

    def _validate_receipt(self, intent, receipt):
        if (not isinstance(receipt, dict) or set(receipt) != {
                "version", "binding", "supervisor", "command", "cleanup", "outcome", "execution", "result"}
                or type(receipt["version"]) is not int or receipt["version"] != 1
                or digest(receipt["binding"]) != digest(self.binding)
                or digest(receipt["supervisor"]) != digest(intent["supervisor"])
                or (intent["command"] is not None and digest(receipt["command"]) != digest(intent["command"]))
                or digest(receipt["cleanup"]) != digest({"backend": "linux-subreaper", "echild": True})
                or receipt["outcome"] not in {"exited", "timeout", "cancelled", "launch-failed"}
                or not isinstance(receipt["result"], dict)
                or not isinstance(receipt["execution"], dict)
                or set(receipt["execution"]) != {"exit_code", "duration_s"}):
            raise CorruptRecord("terminal check does not match its owned execution")
        if receipt["command"] is not None:
            self._identity(receipt["command"])
        execution = receipt["execution"]
        if ((execution["exit_code"] is not None and type(execution["exit_code"]) is not int)
                or type(execution["duration_s"]) not in (int, float)
                or not math.isfinite(execution["duration_s"]) or execution["duration_s"] < 0):
            raise CorruptRecord("invalid owned completion facts")
        result = receipt["result"]
        self._result(result)
        if result.get("authority") != self.binding["check_authority"] or result.get("status") not in {
                "passed", "failed", "void", "unverified", "refused", "skipped", "cancelled"}:
            raise CorruptRecord("invalid acceptance receipt verdict")
        if result["status"] == "passed" and (receipt["outcome"] != "exited" or receipt["command"] is None
                or type(result.get("exit_code")) is not int or result["exit_code"] != 0
                or execution["exit_code"] != 0
                or result.get("fingerprint") != self.binding["candidate"]):
            raise CorruptRecord("passed check has no matching candidate or command")
        if intent["receipt_digest"] is not None and intent["receipt_digest"] != digest(receipt):
            raise CorruptRecord("terminal check receipt digest differs")

    @staticmethod
    def _result(result):
        required = {"run", "authority", "cwd", "mode", "started_at", "status"}
        optional = {"exit_code", "fingerprint", "output_tail", "duration_s", "reason", "code",
                    "infra_changed", "infra_changed_total", "infra_changed_complete"}
        if not required <= set(result) or set(result) - required - optional or result["mode"] != "host":
            raise CorruptRecord("invalid terminal result fields")
        for key, maximum in {"run": 512, "cwd": 32768, "output_tail": 2000,
                             "reason": 2000, "code": 128}.items():
            if key in result and (not isinstance(result[key], str) or len(result[key]) > maximum
                                  or (key in {"run", "cwd", "code"} and "\0" in result[key])):
                raise CorruptRecord("invalid terminal result text")
        for key in ("started_at", "duration_s"):
            if key in result and (type(result[key]) not in (int, float)
                                  or not math.isfinite(result[key]) or result[key] < 0):
                raise CorruptRecord("invalid terminal result timing")
        if ("exit_code" in result and result["exit_code"] is not None
                and type(result["exit_code"]) is not int):
            raise CorruptRecord("invalid terminal result exit code")
        if "fingerprint" in result:
            value = result["fingerprint"]
            if not isinstance(value, str) or len(value) != 16 or any(c not in "0123456789abcdef" for c in value):
                raise CorruptRecord("invalid terminal result fingerprint")
        if "infra_changed" in result and (not isinstance(result["infra_changed"], list)
                or len(result["infra_changed"]) > 200
                or any(not isinstance(path, str) or "\0" in path for path in result["infra_changed"])):
            raise CorruptRecord("invalid terminal infrastructure summary")
        if "infra_changed_total" in result and (type(result["infra_changed_total"]) is not int
                                                or result["infra_changed_total"] < 0):
            raise CorruptRecord("invalid terminal infrastructure count")
        if "infra_changed_complete" in result and type(result["infra_changed_complete"]) is not bool:
            raise CorruptRecord("invalid terminal infrastructure coverage")

    def reconcile(self):
        with self.locked() as (directory, _):
            phase = self._phase(directory)
            intent = self._read_intent(directory)
            receipt = read_record(directory, self.receipt_name)
            if receipt is not None:
                if intent is None or digest(receipt.get("binding")) != digest(self.binding):
                    raise CorruptRecord("receipt has no matching parent intent")
                self._validate_receipt(intent, receipt)
                summary = {key: value for key, value in receipt["result"].items()
                           if key in {"authority", "status", "code", "fingerprint"}}
                return {"classification": "terminal", "receipt_digest": digest(receipt), "result": summary}
            if intent is None:
                return {"classification": "uncertain" if self.binding["attempt_id"] in phase["attempts"]
                        else "absent"}
            return {"classification": "never-launched" if intent["phase"] == "prepared" else "uncertain"}
