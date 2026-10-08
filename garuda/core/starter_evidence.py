"""Bounded selected-session evidence used by the starter read model."""

from __future__ import annotations

import re
from pathlib import Path

from garuda.core import read_model
from garuda.core.session_records import RecordError, list_json, parse_json, read_bytes, read_json
from garuda.flows import artifacts as art


def flow_records(store, sid, declared):
    base = Path(sid) / 'flow'
    valid, invalid, missing, journal_status = [], [], [], 'unknown'
    total = None
    try:
        names = list_json(store, base / 'receipts')
        total = len(names)
        for name in names:
            try:
                record = read_json(store, base / 'receipts' / name)
                if (record.get('step') not in declared or type(record.get('attempt')) is not int
                        or record['attempt'] < 1 or name != f"{record['step']}-{record['attempt']}.json"
                        or not isinstance(record.get('inputs'), list) or not isinstance(record.get('outputs'), list)
                        or any(not isinstance(ref, dict) for ref in record['inputs'] + record['outputs'])
                        or not isinstance(record.get('members', []), list)
                        or any(not isinstance(m, dict) for m in record.get('members', []))
                        or not isinstance(record.get('no_edits', {}), dict)
                        or record.get('status') not in {'done', 'stopped', 'quarantined'}):
                    raise RecordError('unsupported receipt')
                valid.append(record)
            except (ValueError, TypeError):
                invalid.append(name)
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
    except (RecordError, TypeError):
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


def artifact(store, sid, receipt, record):
    """Validate historical bytes through the artifact owner, without Git probes."""
    result = {**{k: record.get(k) for k in art.ArtifactRef.__dataclass_fields__},
              'flow_session': sid, 'evidence': 'unknown',
              'current_workspace_checked': False}
    try:
        step, attempt = receipt['step'], receipt['attempt']
        members = [receipt.get('session_id')] + [m.get('session_id') for m in receipt.get('members', [])]
        if (record.get('type') not in art.ARTIFACTS or type(record.get('version')) is not int
                or record['version'] != art.ARTIFACT_VERSION
                or record.get('producer_step') != step or type(record.get('attempt')) is not int
                or record['attempt'] != attempt or record.get('producer_session') not in members
                or not isinstance(record.get('digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', record['digest'])
                or type(record.get('size')) is not int or not 0 <= record['size'] <= art.MAX_ARTIFACT_CHARS * 4
                or record.get('path') != f"artifacts/{step}-{attempt}-{record['type']}.txt"):
            raise RecordError('artifact identity does not match its receipt')
        root = store.root.resolve() / sid / 'flow'
        if root.resolve() != root:
            raise RecordError('artifact flow directory is symlinked')
        ref = art.ArtifactRef.from_dict(record)
        content = art.load(root, ref, workspace_version=receipt.get('workspace_version_after'))
        if len(content) > art.MAX_ARTIFACT_CHARS:
            raise RecordError('artifact exceeds its text budget')
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
