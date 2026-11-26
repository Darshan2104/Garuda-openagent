"""A file-backed approval channel (plan task B.8, #157).

Another process — ``garuda approvals answer``, a team orchestrator, a script —
can answer a session's parked approval through files in
``<session dir>/approvals/``, alongside the terminal and the dashboard. The
broker stays the one place a decision is made.

**Request.** When the broker parks an approval it publishes
``<id>.request.json``: session, approval id, action, family, runtime, a random
nonce, an expiry and a fingerprint of the permission ceiling, plus the
SHA-256 *digest* of all of those. Owner-only, created exclusively without
following symlinks, file and directory fsynced.

**Answer.** An answer names the session, approval id, request digest and
nonce, and says allow or deny. The writer fills a temporary file, fsyncs it,
then ``link``s it to ``<id>.answer.json``: the name appears complete or not
at all, and a second writer finds it taken. The broker accepts an answer only
if it is a regular, owner-only file that is not a symlink and every binding
matches, before the expiry, with the ceiling unchanged. Anything else — a
replayed answer, another session's or request's, a modified request, a
symlink, an expired or partial answer, a ceiling change — is a denial,
recorded with the reason.

**Owner.** The request names the process that published it (``owner``: pid and
start identity), the only process that can consume an answer. An answer to a
request whose owner is confirmed dead is refused (:class:`OwnerGone`, #807):
nothing would ever act on it. ``garuda runtime recover`` withdraws such a
request (decision ``cancelled``). A request written before owners were
recorded carries none and is answered as before.

**One decision.** The outcome is recorded once in ``<id>.decision.json``
(exclusive creation): a terminal answer and a file answer race to the broker
and only the first counts.

**Delivery.** Before the decision is handed to the runtime the broker
reserves the delivery (``<id>.delivery.json``, ``reserved``); after the
runtime has it, ``acknowledged``. A crash in between leaves ``reserved``,
and a decision whose delivery is reserved is never sent again — nothing here
can prove the runtime's acknowledgement is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

VERSION = 1


class ChannelError(Exception):
    """The channel could not publish or record; fail closed."""


class UnsafeApprovalId(ChannelError):
    """An approval id that could name a file outside the session's approvals directory."""


def ceiling_fingerprint(engine) -> str:
    """A digest of what the permission ceiling would decide."""
    parts = []
    for name in ("_mode", "_tool_rules", "_bash_rules", "_path_rules", "_allow_prefixes",
                 "_deny_bash", "_ask_bash", "_deny_paths", "_ask_paths", "_workspace_root"):
        parts.append(repr(getattr(engine, name, None)))
    for name in ("_parent", "_child"):
        policy = getattr(engine, name, None)
        if policy is not None:
            parts.append(ceiling_fingerprint(policy))
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def _canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def request_digest(fields: dict) -> str:
    return hashlib.sha256(_canonical(fields)).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_exclusive(path: Path, data: dict) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, _canonical(data))
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def _replace(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, _canonical(data))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def read_owned(path: Path) -> dict:
    """A regular, owner-only, non-symlink JSON file of ours, or ``ValueError``."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("not owner-only")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        raw = b""
        while chunk := os.read(fd, 65536):
            raw += chunk
            if len(raw) > 1 << 20:
                raise ValueError("too large")
    finally:
        os.close(fd)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError("not complete JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def _publishing_owner() -> dict:
    """This process, the one that will consume the answer (#807)."""
    from garuda.runtime.recovery import _process_identity

    pid = os.getpid()
    try:
        identity = _process_identity(pid) or "unknown"
    except Exception:
        identity = "unknown"
    return {"pid": pid, "identity": identity}


def owner_gone(request: dict) -> bool:
    """True only when the request's recorded owner is confirmed dead.

    A request with no recorded owner, or whose owner's liveness cannot be
    determined, is not called gone: this only ever refuses, it never allows.
    """
    owner = request.get("owner")
    if not isinstance(owner, dict):
        return False
    from garuda.runtime.ownership import owner_liveness

    # pid and start identity only: the owner process holds the broker; its
    # process group or descendants cannot consume an answer.
    return owner_liveness({"pid": owner.get("pid"), "identity": owner.get("identity")}) is False


@dataclass(frozen=True)
class Published:
    approval_id: str
    digest: str
    nonce: str
    expires_at: float
    ceiling: str


class FileApprovalChannel:
    """One session's approval files. Used by the broker; see the module docstring."""

    def __init__(self, directory: str | Path, session_id: str):
        self.directory = Path(directory)
        self.session_id = session_id

    def _path(self, approval_id: str, kind: str) -> Path:
        if not approval_id or "/" in approval_id or approval_id.startswith("."):
            raise UnsafeApprovalId(f"unsafe approval id {approval_id!r}")
        return self.directory / f"{approval_id}.{kind}.json"

    def _ensure_dir(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(self.directory)
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ChannelError(f"{self.directory} is not an owner-only directory")

    # -- broker side -------------------------------------------------------------

    def publish(self, request, *, ceiling: str, expires_at: float) -> Published:
        """Write the request file. A failure means no file channel for it."""
        self._ensure_dir()
        fields = {
            "version": VERSION,
            "session_id": self.session_id,
            "approval_id": request.approval_id,
            "action": request.action,
            "family": request.family,
            "runtime_id": request.runtime_id,
            "nonce": secrets.token_hex(16),
            "expires_at": round(expires_at, 3),
            "ceiling": ceiling,
            "owner": _publishing_owner(),
        }
        if getattr(request, "requester", None) is not None:
            fields.update(requester=request.requester, requester_ceiling=request.requester_ceiling)
        digest = request_digest(fields)
        try:
            _create_exclusive(self._path(request.approval_id, "request"),
                              {**fields, "digest": digest})
        except OSError as exc:
            raise ChannelError(f"could not publish approval {request.approval_id}: {exc}") from exc
        return Published(request.approval_id, digest, fields["nonce"], fields["expires_at"],
                         ceiling)

    def poll(self, published: Published, *, ceiling_now: str, now: float | None = None):
        """``None`` while unanswered; else ``(allow, reason)``. Invalid denies."""
        path = self._path(published.approval_id, "answer")
        try:
            answer = read_owned(path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            return False, f"invalid answer file: {exc}"
        moment = time.time() if now is None else now
        checks = (
            (answer.get("version") == VERSION, "unknown answer version"),
            (answer.get("session_id") == self.session_id, "answer is for another session"),
            (answer.get("approval_id") == published.approval_id, "answer is for another request"),
            (answer.get("request_digest") == published.digest,
             "answer does not match this request (replayed or modified)"),
            (answer.get("nonce") == published.nonce, "answer nonce does not match"),
            (isinstance(answer.get("allow"), bool), "answer has no allow/deny"),
            (moment <= published.expires_at, "answer arrived after the request expired"),
            (ceiling_now == published.ceiling, "the permission ceiling changed"),
        )
        for ok, reason in checks:
            if not ok:
                return False, f"rejected file answer: {reason}"
        return answer["allow"], None

    def decide(self, approval_id: str, outcome: str, via: str, reason: str | None) -> bool:
        """Record the single decision. ``False`` when one already exists."""
        self._ensure_dir()
        try:
            _create_exclusive(self._path(approval_id, "decision"), {
                "version": VERSION, "session_id": self.session_id, "approval_id": approval_id,
                "outcome": outcome, "via": via, "reason": reason, "decided_at": time.time(),
            })
        except FileExistsError:
            return False
        except OSError as exc:
            raise ChannelError(f"could not record decision for {approval_id}: {exc}") from exc
        return True

    def reserve_delivery(self, approval_id: str) -> None:
        """Before the runtime is told. A reservation already present refuses."""
        try:
            _create_exclusive(self._path(approval_id, "delivery"), {
                "version": VERSION, "approval_id": approval_id, "state": "reserved",
                "at": time.time(),
            })
        except FileExistsError as exc:
            raise ChannelError(
                f"approval {approval_id} was already delivered or its delivery is unknown; "
                "it is never sent twice"
            ) from exc

    def acknowledge(self, approval_id: str) -> None:
        _replace(self._path(approval_id, "delivery"), {
            "version": VERSION, "approval_id": approval_id, "state": "acknowledged",
            "at": time.time(),
        })

    def unacknowledged(self) -> list[str]:
        """Approvals whose delivery was reserved but never acknowledged (a crash)."""
        if not self.directory.is_dir():
            return []
        out = []
        for path in sorted(self.directory.glob("*.delivery.json")):
            try:
                if read_owned(path).get("state") == "reserved":
                    out.append(path.name[: -len(".delivery.json")])
            except (OSError, ValueError):
                out.append(path.name[: -len(".delivery.json")])
        return out

    def pending(self) -> list[dict]:
        """Requests with no decision yet, for ``garuda approvals list``."""
        if not self.directory.is_dir():
            return []
        out = []
        for path in sorted(self.directory.glob("*.request.json")):
            approval_id = path.name[: -len(".request.json")]
            if self._path(approval_id, "decision").exists():
                continue
            try:
                out.append(read_owned(path))
            except (OSError, ValueError):
                continue
        return out


def withdraw_orphaned(directory: str | Path, session_id: str, *, session_owner_dead: bool,
                      reason: str) -> list[dict]:
    """Record ``cancelled`` for each undecided request whose owner is confirmed dead.

    A request without a recorded owner is withdrawn only when the session's own
    owner is confirmed dead (``session_owner_dead``). The decision is created
    exclusively, so a live broker deciding concurrently still wins, and its own
    exclusive decision then fails closed (deny). Returns the withdrawn requests.
    """
    channel = FileApprovalChannel(directory, session_id)
    withdrawn = []
    for request in channel.pending():
        approval_id = request.get("approval_id")
        if not isinstance(approval_id, str):
            continue
        if not (owner_gone(request)
                or (session_owner_dead and not isinstance(request.get("owner"), dict))):
            continue
        if channel.decide(approval_id, "cancelled", "recover", reason):
            withdrawn.append(request)
    return withdrawn


WITHDRAWN_REASON = ("the run's process exited before an answer; withdrawn by "
                    "`garuda runtime recover`")


def withdraw_dead_session_approvals(store, session_id: str) -> list[str]:
    """``garuda runtime recover``: withdraw a dead run's parked approvals (#807).

    Each withdrawal is an exclusive ``cancelled`` decision (via ``recover``) and
    an ``approval:<id>`` audit record. Returns notes for the recovery report.
    """
    from garuda.runtime.ownership import owner_liveness

    try:
        meta = store.load_meta(session_id)
    except Exception as exc:
        return [f"parked approvals were not inspected: session unreadable ({exc})"]
    state = meta.get("state") if isinstance(meta, dict) else None
    owner = state.get("owner") if isinstance(state, dict) else None
    owner_dead = isinstance(owner, dict) and owner_liveness(owner) is False
    try:
        withdrawn = withdraw_orphaned(Path(store.session_dir(session_id)) / "approvals",
                                      session_id, session_owner_dead=owner_dead,
                                      reason=WITHDRAWN_REASON)
    except (ChannelError, OSError) as exc:
        return [f"parked approvals were not withdrawn: {exc}"]
    notes = []
    for request in withdrawn:
        approval_id = request["approval_id"]
        try:
            from garuda.acp.broker import ApprovalRecord
            from garuda.core.approval_outcome import ApprovalOutcome

            record = ApprovalRecord(
                approval_id=approval_id, outcome=ApprovalOutcome.CANCELLED,
                action=str(request.get("action", "")), family=str(request.get("family", "")),
                runtime_id=str(request.get("runtime_id", "")), via="recover",
                reason=WITHDRAWN_REASON)
            store.update_meta(session_id, {f"approval:{approval_id}": record.to_dict()})
        except Exception:
            notes.append(f"withdrawn approval {approval_id} was not audited in the session record")
        notes.append(f"withdrew parked approval {approval_id}: its run's process is gone")
    return notes


class AnswerRefused(ValueError):
    """An answer that must not be written; ``code`` says why (stable, for callers)."""

    code = "refused"


class StaleRequest(AnswerRefused):
    """The request is not the one the answerer saw (replaced, or its digest differs)."""

    code = "stale_request"


class RequestExpired(AnswerRefused):
    code = "expired"


class OwnerGone(AnswerRefused):
    """The process that parked the request is dead: no one would consume an answer (#807)."""

    code = "owner_gone"


def write_answer(directory: str | Path, session_id: str, approval_id: str, allow: bool,
                 *, expect_digest: str | None = None) -> dict:
    """Answer a parked approval from another process (``garuda approvals answer``).

    Binds the answer to the request as published. Exclusive: a second answer,
    from anyone, raises ``FileExistsError``. ``expect_digest`` is the digest of
    the request the answerer was shown (a dashboard page): if the request on
    disk is not that one, the answer is refused as :class:`StaleRequest` rather
    than binding a decision to something its author never saw. A request that
    already has a decision is not answerable, nor is one whose owner process
    is confirmed dead (:class:`OwnerGone`).
    """
    channel = FileApprovalChannel(directory, session_id)
    request = read_owned(channel._path(approval_id, "request"))
    fields = {k: v for k, v in request.items() if k != "digest"}
    if request_digest(fields) != request.get("digest"):
        raise AnswerRefused("the request file was modified")
    if request.get("session_id") != session_id:
        raise AnswerRefused("the request belongs to another session")
    if expect_digest is not None and expect_digest != request["digest"]:
        raise StaleRequest("the request changed since it was shown")
    if time.time() > float(request.get("expires_at", 0)):
        raise RequestExpired("the request has expired")
    if channel._path(approval_id, "decision").exists():
        raise FileExistsError(f"approval {approval_id} already has a decision")
    if owner_gone(request):
        raise OwnerGone(
            f"the run that parked it (pid {request['owner'].get('pid')}) has exited, so no one "
            "would act on an answer; `garuda runtime recover` withdraws it")
    answer = {
        "version": VERSION, "session_id": session_id, "approval_id": approval_id,
        "request_digest": request["digest"], "nonce": request["nonce"], "allow": bool(allow),
        "answered_at": time.time(),
    }
    target = channel._path(approval_id, "answer")
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            os.write(fd, _canonical(answer))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(tmp, target)  # appears complete, once; a second writer gets EEXIST
    finally:
        os.unlink(tmp)
    _fsync_dir(target.parent)
    return answer
