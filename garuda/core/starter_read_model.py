"""Selected starter evidence projected through the shared session read model."""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path

from garuda.core import read_model, starter_evidence
from garuda.core.session_records import (
    ReadOnlySessions,
    RecordError,
)
from garuda.runtime import session_state


def _redact(value):
    from garuda.context.redact import redact_text

    if isinstance(value, str):
        return redact_text(value)[0]
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _starter(meta):
    from garuda.scenarios.catalog import load_catalog
    from garuda.scenarios.digests import digest
    from garuda.scenarios.inputs import validate_inputs

    data = meta.get('starter')
    if data is None:
        return None, 'legacy'
    try:
        entries = load_catalog()
        if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
                or type(data.get('starter_version')) is not int or data['starter_version'] != 1
                or data.get('starter_id') not in entries or not isinstance(data.get('inputs'), dict)
                or data.get('inputs_sha256') != digest(data['inputs'])
                or not isinstance(meta.get('task'), str)
                or data.get('task_sha256') != hashlib.sha256(meta['task'].encode()).hexdigest()
                or not isinstance(data.get('approved_scope'), list)
                or data.get('approved_scope_sha256') != digest(data['approved_scope'])):
            return None, 'unknown'
        validate_inputs(entries[data['starter_id']], data['inputs'])
        return data, 'valid'
    except (ValueError, TypeError, KeyError):
        return None, 'unknown'


def project(store, session_id, *, limit=50, offset=0):
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise ValueError('result pages need limit 1..200 and a non-negative offset')
    store = ReadOnlySessions(store.root)
    coverage = {'scope': 'selected-session', 'session_id': session_id, 'limit': limit, 'offset': offset, 'complete': False}
    try:
        meta = store.load_meta(session_id)
        if meta.get('session_id') != session_id:
            raise RecordError('selected session identity does not match')
    except (ValueError, TypeError):
        return _redact({'session_id': session_id, 'state': {'process': 'unknown', 'work': 'unknown', 'outcome': 'unknown'},
                'verification': {'status': 'unknown'}, 'review': {'status': 'unknown'}, 'coverage': coverage,
                'next_action': {'id': 'inspect-records', 'label': 'Inspect missing or unreadable session evidence'}})
    state_valid = True
    try:
        if 'state' in meta:
            if (not isinstance(meta['state'], dict) or type(meta['state'].get('version')) is not int
                    or meta['state'].get('version') != session_state.STATE_VERSION):
                raise ValueError('unsupported state')
            session_state.validate(meta['state'])
        row = read_model.session_row(store, meta, liveness=lambda _owner: None)
    except (ValueError, TypeError, AttributeError, KeyError):
        state_valid = False
        row = {'session_id': session_id, 'workspace': meta.get('workspace'),
               'state': {'process': 'unknown', 'work': 'unknown', 'outcome': 'unknown'},
               'verification': {'status': 'unknown'}}
    data, integrity = _starter(meta)
    inputs = data['inputs'] if data else {}
    row.update(starter_id=data['starter_id'] if data else None, starter_metadata=integrity,
               goal=inputs.get('goal', inputs.get('feedback', inputs.get('question'))),
               constraints=inputs.get('constraints'), requirements=inputs.get('requirements'), exclude=inputs.get('exclude'),
               approved_scope=data.get('approved_scope', []) if data else [], supplied_sources=data.get('sources', []) if data else [],
               source_scope='Supplied context; this is not evidence the runtime read a named file.',
               process_evidence='Stored state; active-owner liveness is not probed.', review=None, review_label='no review')
    withheld = meta.get('outputs_withheld') is True
    row['output'] = {'text': None if withheld else meta.get('final_message'),
                     'scope': 'withheld' if withheld else 'stored-summary', 'may_be_clipped': True}
    identities, artifacts, selected = [], [], []
    if meta.get('kind') == 'flow':
        row['runtime'] = None
        row['model'] = None
        flow_meta = meta.get('flow') if isinstance(meta.get('flow'), dict) else {}
        declared = flow_meta.get('steps', [])
        if not isinstance(declared, list) or any(not isinstance(s, str) for s in declared) or len(set(declared)) != len(declared):
            declared = []
        records, details = starter_evidence.flow_records(store, session_id, declared)
        selected = records[offset:offset + limit]
        coverage.update(details, returned_records=len(selected), next_offset=offset + limit if offset + limit < len(records) else None)
        coverage['complete'] = (bool(declared) and details['total_records'] is not None and not details['unreadable_records']
                                and not details['missing_records'] and details['journal'] == 'readable'
                                and offset == 0 and len(selected) == len(records))
        if row['state']['outcome'] == 'completed' and set(declared) - {r['step'] for r in records if r['status'] == 'done'}:
            coverage['complete'] = False
        row['flow'] = read_model.flow(store, session_id, {**meta, 'flow': {**flow_meta, 'steps': declared}}, receipts=selected)
        coverage['unreadable_artifacts'] = 0
        for receipt in selected:
            producers = [receipt.get('session_id')] if receipt.get('session_id') else [m.get('session_id') for m in receipt.get('members', [])]
            for sid in producers:
                identity, valid = starter_evidence.identity(store, sid, step=receipt['step'], attempt=receipt['attempt'],
                                            flow_session=session_id, project_id=meta.get('project_id'))
                identities.append(identity)
                coverage['complete'] &= valid
            for ref in receipt['outputs']:
                artifact, valid = starter_evidence.artifact(store, session_id, receipt, ref)
                artifacts.append(artifact)
                coverage['unreadable_artifacts'] += int(not valid)
                coverage['complete'] &= valid
        recorded = meta.get('review') if isinstance(meta.get('review'), dict) else None
        policies = meta.get('review_policies', [])
        independence = (recorded or {}).get('independence')
        policy = independence.get('policy') if isinstance(independence, dict) else None
        if isinstance(policies, list) and any(isinstance(p, dict) and p.get('policy') == 'waived' for p in policies):
            policy = 'waived'
        elif not policy and isinstance(policies, list) and policies:
            policy = 'required' if all(isinstance(p, dict) and p.get('policy') == 'required' for p in policies) else 'unknown'
        row['review_label'] = ('review not independent' if policy == 'waived' else
                               'independent review' if policy == 'required' and recorded else
                               'independent review configured' if policy == 'required' else
                               'review policy unknown' if recorded or policy else 'no review')
        row['review'] = recorded if coverage['complete'] else {'status': 'unknown', 'recorded': recorded}
        # P1a flows have no acceptance-check owner. A stored review cannot verify.
        row['verification'] = {'status': 'unavailable'}
    else:
        identity, valid = starter_evidence.identity(store, session_id)
        row['runtime'] = identity['runtime_id']
        row['model'] = identity['model_id']
        receipts, receipts_valid = starter_evidence.acceptance(meta, row['verification'])
        row['acceptance_receipts'] = receipts
        row['verification_evidence'] = 'Stored historical owner verdict and inline acceptance receipts; current workspace is not probed.'
        if not receipts_valid:
            row['verification'] = {'status': 'unknown', 'recorded': row['verification']}
        valid &= receipts_valid
        identities.append(identity)
        coverage.update(total_records=1, readable_records=int(valid), returned_records=1, complete=valid)
    coverage['child_sessions'] = {'returned': len(identities),
                                  'readable': sum(i['evidence'] != 'unknown' for i in identities)}
    coverage['complete'] &= state_valid
    row.update(identities=identities, artifacts=artifacts, coverage=coverage)
    row['approvals'], row['approvals_evidence'] = starter_evidence.approvals(store, session_id)
    coverage['complete'] &= row['approvals_evidence']['status'] == 'readable'
    if withheld or not coverage['complete'] or integrity == 'unknown':
        action = {'id': 'inspect-records', 'label': 'Inspect incomplete or unreadable evidence'}
    elif row['approvals']:
        action = {'id': 'approval', 'label': 'Inspect the parked approval', 'command': f'garuda approvals list {shlex.quote(session_id)}'}
    elif row['state']['work'] in session_state.ACTIVE_WORK:
        action = {'id': 'monitor', 'label': 'Follow the existing session', 'command': f'garuda sessions show {shlex.quote(session_id)}'}
    elif row['review'] and row['review'].get('status') == 'review_changes_requested':
        action = {'id': 'inspect-review', 'label': 'Inspect failed review', 'command': f'garuda flow show {shlex.quote(session_id)}'}
    elif data and data['starter_id'] in {'plan-change', 'plan-feedback'} and row['state']['outcome'] == 'completed' and (plans := [a for a in artifacts if a.get('type') == 'plan']):
        plan = plans[-1]
        reference = f"{session_id}:{plan.get('producer_step')}:{plan.get('attempt')}"
        from garuda.context.tags import TagError
        from garuda.core.project_identity import ProjectIdentityError
        from garuda.scenarios.handoff import resolve_plan
        try:
            resolve_plan(reference, Path(meta['workspace']), store)
        except (TagError, ProjectIdentityError, ValueError, OSError, KeyError, TypeError) as exc:
            # The handoff owner is the authority for an implementable plan.
            action = {'id': 'inspect-records', 'label': 'Inspect plan handoff evidence',
                      'code': getattr(exc, 'code', 'starter.plan_invalid')}
        else:
            action = {'id': 'implement-plan', 'label': 'Implement this plan explicitly', 'plan_artifact': reference,
                  'command': shlex.join(['garuda', 'starter', 'run', 'build-review', '--variant', 'pair', '--plan-artifact', reference, '--workspace', meta['workspace']])}
    elif row['verification']['status'] == 'unavailable' and (not data or data['starter_id'] != 'ask-role'):
        action = {'id': 'configure-checks', 'label': 'Configure checks or use Build and check', 'command': 'garuda init --project'}
    else:
        action = {'id': 'inspect', 'label': 'Inspect recorded results', 'command': f'garuda sessions show {shlex.quote(session_id)}'}
    row['next_action'] = action
    return _redact(row)
