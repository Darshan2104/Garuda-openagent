"""Bounded selected-session evidence used by the starter read model."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from garuda.core import read_model
from garuda.core.session_records import (
    BoundedSessionRecords,
    ReadBudgetExceeded,
    RecordError,
    list_json,
    parse_json,
    read_bytes,
    read_json,
)
from garuda.flows import artifacts as art


def flow_steps(meta):
    """Validated declared step IDs, or unknown declarations without adaptation."""
    flow = meta.get('flow')
    steps = flow.get('steps') if isinstance(flow, dict) else None
    if (not isinstance(steps, list)
            or any(not isinstance(step, str) or not step for step in steps)
            or len(set(steps)) != len(steps)):
        return None
    return list(steps)


def flow_receipt_shape(record, declared):
    """The nested receipt shape shared by evidence and session rendering."""
    shape = (isinstance(record, dict)
            and isinstance(record.get('step'), str)
            and record.get('step') in declared and type(record.get('attempt')) is int
            and record['attempt'] > 0
            and isinstance(record.get('inputs'), list) and isinstance(record.get('outputs'), list)
            and all(isinstance(ref, dict) for ref in record['inputs'] + record['outputs'])
            and isinstance(record.get('members', []), list)
            and all(isinstance(member, dict) for member in record.get('members', []))
            and isinstance(record.get('no_edits', {}), dict)
            and isinstance(record.get('status'), str)
            and record.get('status') in {'done', 'stopped', 'quarantined'})
    if not shape:
        return False
    from garuda.core.sessions import validate_session_ref

    for linked in [record, *record.get('members', [])]:
        for key in ('session_id', 'session'):
            value = linked.get(key)
            if value is not None:
                try:
                    validate_session_ref(value)
                except ValueError:
                    return False
    return True


def selected_flow_records(store, sid, declared, *, expected_project_id=None):
    """Bounded receipt discovery shared by flow and consult-session rendering."""
    from garuda.core.artifact_read_model import MAX_ARTIFACT_SOURCE_BYTES

    records = store if isinstance(store, BoundedSessionRecords) else BoundedSessionRecords(
        store.root, byte_budget=MAX_ARTIFACT_SOURCE_BYTES,
    )
    with records.selected_session(sid):
        meta = records.load_meta(sid)
        if (meta.get('session_id') != sid or expected_project_id is not None
                and meta.get('project_id') != expected_project_id):
            raise RecordError('flow receipt source does not match the selected session')
        return flow_records(records, sid, declared)


def flow_records(store, sid, declared):
    base = Path(sid) / 'flow'
    valid, invalid, missing, journal_status = [], [], [], 'unknown'
    total = None
    try:
        names = list_json(store, base / 'receipts')
        total = len(names)
        for name in names:
            try:
                record = parse_json(store.read(base / 'receipts' / name)) \
                    if isinstance(store, BoundedSessionRecords) \
                    else read_json(store, base / 'receipts' / name)
                if (not flow_receipt_shape(record, declared)
                        or name != f"{record['step']}-{record['attempt']}.json"):
                    raise RecordError('unsupported receipt')
                valid.append(record)
            except (ValueError, TypeError):
                invalid.append(name)
        if isinstance(store, BoundedSessionRecords):
            journal_path = Path(store.root) / base / 'journal.jsonl'
            journal_info = os.lstat(journal_path)
            if stat.S_ISLNK(journal_info.st_mode) or not stat.S_ISREG(journal_info.st_mode):
                raise RecordError('flow journal is not a regular file')
            journal = store.read(base / 'journal.jsonl')
        else:
            journal = read_bytes(store, base / 'journal.jsonl')
        if journal and not journal.endswith(b'\n'):
            raise RecordError('torn journal tail')
        events = [parse_json(line) for line in journal.splitlines() if line.strip()]
        links = {}
        for i, event in enumerate(events):
            if event.get('event') not in {'intent', 'receipt'}:
                continue
            if event.get('step') not in declared or type(event.get('attempt')) is not int or event['attempt'] < 1:
                raise RecordError('unsupported journal linkage')
            key = (event['step'], event['attempt'])
            links.setdefault(key, {'intent': [], 'receipt': []})[event['event']].append(i)
        known = {(r['step'], r['attempt']) for r in valid}
        for key, positions in links.items():
            if len(positions['intent']) != 1 or len(positions['receipt']) > 1:
                raise RecordError('ambiguous journal linkage')
            if positions['receipt'] and positions['receipt'][0] <= positions['intent'][0]:
                raise RecordError('out-of-order journal linkage')
            if not positions['receipt'] or key not in known:
                missing.append({'step': key[0], 'attempt': key[1]})
        if any(key not in links or len(links[key]['receipt']) != 1 for key in known):
            raise RecordError('receipt lacks journal authority')
        journal_status = 'readable'
    except (RecordError, TypeError, OSError):
        journal_status = 'unknown'
    valid.sort(key=lambda r: (declared.index(r['step']), r['attempt']))
    return valid, {'total_records': total, 'readable_records': len(valid), 'unreadable_records': len(invalid),
                   'unreadable_files': invalid, 'missing_records': missing, 'journal': journal_status}


def identity(store, sid, *, step=None, attempt=None, flow_session=None, project_id=None):
    try:
        meta = store.load_meta(sid)
        from garuda.runtime.session import SESSION_SCHEMA_VERSION

        if ('schema_version' in meta and (type(meta['schema_version']) is not int
                                         or meta['schema_version'] != SESSION_SCHEMA_VERSION)):
            raise RecordError('unsupported session schema')
        if meta.get('session_id') != sid:
            raise RecordError('child identity is missing')
        if flow_session is not None and (meta.get('project_id') != project_id or
                meta.get('flow_step') != {'flow_session': flow_session, 'step': step, 'attempt': attempt}):
            raise RecordError('child lineage does not match its receipt')
        segments = meta.get('runtime_segments') or []
        role = meta.get('role') or {}
        if not segments and (meta.get('state') or {}).get('work') == 'queued':
            return {'session_id': sid, 'step': step, 'attempt': attempt, 'runtime_id': None,
                    'model_id': None, 'evidence': 'not-started'}, True
        if segments and segments[-1].get('kind') not in {'native', 'acp'}:
            raise RecordError('unsupported runtime segment kind')
        if not segments and not meta.get('model') and meta.get('starter') is not None:
            raise RecordError('actual runtime has not been recorded')
        runtime = segments[-1].get('runtime_id') if segments else 'native'
        if not isinstance(runtime, str) or not runtime:
            raise RecordError('runtime identity is unreadable')
        if segments and segments[-1].get('kind') == 'acp':
            from garuda.runtime.roles import PROVEN_OPTIONS

            adapter = role.get('adapter') or {}
            key = PROVEN_OPTIONS.get((runtime, adapter.get('version')), {}).get('model_id')
            model = (role.get('acp_options') or {}).get(key) if key else None
        else:
            model = meta.get('model')
        if model is not None and not isinstance(model, str):
            raise RecordError('model identity is unreadable')
        return {'session_id': sid, 'step': step, 'attempt': attempt, 'role': role.get('name'),
                'runtime_id': runtime, 'model_id': model or None, 'evidence': 'recorded-runtime'}, True
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return {'session_id': sid, 'step': step, 'attempt': attempt, 'runtime_id': None,
                'model_id': None, 'evidence': 'unknown'}, False


def artifact_reference(receipt, record):
    """Validate a declared reference's identity without inspecting payload bytes."""
    step, attempt = receipt['step'], receipt['attempt']
    members = [receipt.get('session_id')] + [m.get('session_id') for m in receipt.get('members', [])]
    if (not isinstance(record.get('producer_session'), str) or not record['producer_session']
            or len(record['producer_session']) > 512
            or not isinstance(step, str) or not step or len(step) > 512
            or type(attempt) is not int or attempt < 1
            or (record.get('workspace_version') is not None
                and not isinstance(record['workspace_version'], str))
            or record.get('type') not in art.ARTIFACTS or type(record.get('version')) is not int
            or record['version'] != art.ARTIFACT_VERSION
            or record.get('producer_step') != step or type(record.get('attempt')) is not int
            or record['attempt'] != attempt or record.get('producer_session') not in members
            or not isinstance(record.get('digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', record['digest'])
            or type(record.get('size')) is not int or not 0 <= record['size'] <= art.MAX_ARTIFACT_CHARS * 4
            or record.get('path') != f"artifacts/{step}-{attempt}-{record['type']}.txt"):
        raise RecordError('artifact identity does not match its receipt')
    return art.ArtifactRef.from_dict(record)


def artifact(store, sid, receipt, record, *, verification_bytes=None):
    """Validate historical bytes through the artifact owner, without Git probes."""
    result = {**{k: record.get(k) for k in art.ArtifactRef.__dataclass_fields__},
              'flow_session': sid, 'evidence': 'unknown',
              'current_workspace_checked': False}
    try:
        ref = artifact_reference(receipt, record)
        step, attempt = ref.producer_step, ref.attempt
        root = store.root.resolve() / sid / 'flow'
        if root.resolve() != root:
            raise RecordError('artifact flow directory is symlinked')
        from garuda.core.artifact_read_model import descriptor, source_capture

        source = source_capture(
            kind="flow_artifact", completeness="complete",
            identity=f"{step}:{attempt}:{record['type']}",
            media_type="text/plain; charset=utf-8", redaction="unknown",
        )
        common = descriptor(
            owner_kind="flow-artifact", owner_reference=record['path'],
            storage_session_id=sid, producer_session_id=record['producer_session'],
            producer=step, source=source, size_bytes=record['size'],
            sha256=record['digest'], size_provenance="recorded",
            digest_provenance="recorded", availability="unknown",
            integrity="unverified", reason="validation_pending",
            retention={"policy": "flow-session-owner"},
        )
        result['artifact'] = common
        if verification_bytes is not None and (
            type(verification_bytes) is not int or verification_bytes < record['size']
        ):
            common['availability'] = {
                'state': 'unknown', 'integrity': 'unverified',
                'reason': 'verification_budget_exhausted',
            }
            return result, False
        try:
            if isinstance(store, BoundedSessionRecords):
                byte_limit = art.MAX_ARTIFACT_CHARS * 4
                if verification_bytes is not None:
                    # Include the reader's reserved overflow block even if the
                    # payload is oversized, corrupt or disappears mid-read.
                    if verification_bytes <= 65536:
                        raise ReadBudgetExceeded("artifact verification allowance exhausted")
                    byte_limit = min(byte_limit, verification_bytes - 65536)
                data = store.read(Path(sid) / 'flow' / ref.path, limit=byte_limit)
                content = art.validate_content(
                    ref, data, workspace_version=receipt.get('workspace_version_after'),
                )
            else:
                content = art.load(root, ref, workspace_version=receipt.get('workspace_version_after'))
        except ReadBudgetExceeded:
            common['availability'] = {
                'state': 'unknown', 'integrity': 'unverified',
                'reason': 'verification_budget_exhausted',
            }
            return result, False
        except RecordError as exc:
            common['availability'] = {
                'state': 'unavailable', 'integrity': 'unknown',
                'reason': 'flow.input_missing' if isinstance(exc.__cause__, FileNotFoundError)
                    else 'flow.input_not_regular',
            }
            return result, False
        except art.ArtifactError as exc:
            common['availability'] = {
                'state': 'unavailable',
                'integrity': 'failed' if exc.code in {'flow.input_forged', 'flow.input_stale'} else 'unknown',
                'reason': exc.code,
            }
            return result, False
        if len(content) > art.MAX_ARTIFACT_CHARS:
            raise RecordError('artifact exceeds its text budget')
        common['availability'] = {'state': 'available', 'integrity': 'verified'}
        result.update(evidence='validated-historical-bytes', content=content)
        return result, True
    except (art.ArtifactError, OSError, ValueError, TypeError, KeyError):
        return result, False


def approvals(store, sid):
    """Snapshot selected approval records before using the shared formatter."""
    from garuda.acp.approval_channel import VERSION, request_digest

    base = Path(sid) / 'approvals'
    directory = store.root / base
    if not directory.exists() and not directory.is_symlink():
        return [], {'status': 'readable', 'total_records': 0}
    try:
        names = list_json(store, base)
        records = {name: read_json(store, base / name) for name in names}
        pending = []
        for name, record in records.items():
            if not name.endswith('.request.json'):
                continue
            aid = name.removesuffix('.request.json')
            fields = {k: v for k, v in record.items() if k != 'digest'}
            if (type(record.get('version')) is not int or record['version'] != VERSION
                    or record.get('session_id') != sid or record.get('approval_id') != aid
                    or record.get('digest') != request_digest(fields)):
                raise RecordError('approval identity is unreadable')
            float(record['expires_at'])
            decision = records.get(aid + '.decision.json')
            if decision is not None and (decision.get('version') != VERSION
                    or decision.get('session_id') != sid or decision.get('approval_id') != aid):
                raise RecordError('approval decision identity is unreadable')
            if decision is None:
                pending.append(record)
        return read_model.approvals(store, sid, requests=pending), {'status': 'readable', 'total_records': len(names)}
    except (ValueError, TypeError, KeyError):
        return [], {'status': 'unknown', 'total_records': None}



def acceptance(meta, recorded):
    """Project inline check receipts through the existing acceptance verdict owner."""
    from garuda.config.garuda_yaml import CLI, PROJECT
    from garuda.core.acceptance import verification_from

    receipts = meta.get('acceptance_receipts')
    try:
        if receipts is None:
            # Older authoritative grader states have no acceptance receipts.
            valid = recorded.get('authority') not in {CLI, PROJECT}
            return None, valid
        if not isinstance(receipts, list) or any(not isinstance(r, dict) for r in receipts):
            raise RecordError('acceptance receipts are unreadable')
        if not receipts and recorded.get('authority') in {CLI, PROJECT}:
            return receipts, False
        if receipts:
            if any(not isinstance(r.get('run'), str) or not isinstance(r.get('authority'), str)
                   or r.get('status') not in {'passed', 'failed', 'void', 'unverified', 'refused', 'skipped'}
                   for r in receipts):
                raise RecordError('unsupported acceptance receipt')
            if any(r['status'] == 'passed' and (type(r.get('exit_code')) is not int
                   or r['exit_code'] != 0 or not isinstance(r.get('fingerprint'), str)
                   or not r['fingerprint']) for r in receipts):
                raise RecordError('passed acceptance receipt lacks successful check evidence')
            if verification_from(receipts) != recorded:
                raise RecordError('acceptance receipts disagree with recorded verification')
        return receipts, True
    except (ValueError, TypeError, AttributeError):
        return None, False
