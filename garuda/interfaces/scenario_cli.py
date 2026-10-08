"""Garuda starter CLI, backed by the shared compiler, service and read model."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import redirect_stdout
from dataclasses import asdict

from garuda.acp.protocol import AcpError
from garuda.context.tags import TagError
from garuda.core.project_identity import ProjectIdentityError
from garuda.flows.engine import FlowStopped
from garuda.interfaces.bg_sessions import BackgroundRefused
from garuda.scenarios.catalog import load_catalog
from garuda.scenarios.compile import compile_scenario
from garuda.scenarios.service import RESOLUTION_ERRORS, StarterService
from garuda.scenarios.types import StarterError
from garuda.workspace.confined_acp import ConfinementRefused

TEXT_FIELDS = ('goal', 'requirements', 'exclude', 'constraints', 'feedback', 'current',
               'desired', 'question', 'role', 'variant', 'plan_artifact')


def add_parser(subparsers):
    parser = subparsers.add_parser('starter', help='Discover, preview and run packaged Garuda starters')
    commands = parser.add_subparsers(dest='starter_command', required=True)
    listing = commands.add_parser('list', help='List starters and configured readiness')
    listing.add_argument('--workspace', default='.')
    listing.add_argument('--json', action='store_true')
    show = commands.add_parser('show', help='Inspect an installed starter')
    show.add_argument('starter_id')
    show.add_argument('--workspace', default='.')
    show.add_argument('--json', action='store_true')
    run = commands.add_parser('run', help='Preview or explicitly start a starter')
    run.add_argument('starter_id')
    run.add_argument('--workspace', default='.')
    run.add_argument('--json', action='store_true')
    run.add_argument('--preview', action='store_true')
    run.add_argument('--allow-cross-project-context', action='store_true')
    for field in TEXT_FIELDS:
        run.add_argument('--' + field.replace('_', '-'))
    run.add_argument('--source', action='append')
    run.add_argument('--name')
    run.add_argument('--isolation', choices=['shared', 'worktree', 'auto'])
    run.add_argument('--bg', action='store_true', default=None)
    run.add_argument('--check', action='append')
    result = commands.add_parser('result', help='Read selected session evidence and next action')
    result.add_argument('session')
    result.add_argument('--json', action='store_true')
    result.add_argument('--workspace', default='.')
    result.add_argument('--limit', type=int, default=50)
    result.add_argument('--offset', type=int, default=0)
    example = commands.add_parser('example', help='Materialize the packaged reconnect example')
    example.add_argument('example', choices=['reconnect'])
    example.add_argument('directory')


def _inputs(args):
    inputs = {name: getattr(args, name) for name in TEXT_FIELDS if getattr(args, name) is not None}
    if args.source is not None:
        inputs['sources'] = args.source
    options = {name: getattr(args, name) for name in ('name', 'isolation', 'bg') if getattr(args, name) is not None}
    if args.check is not None:
        options['checks'] = args.check
    if options:
        inputs['options'] = options
    return inputs


def render(data, *, as_json=False):
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    if isinstance(data, list):
        for row in data:
            state = row['readiness']
            print(f"{row['id']:<18} {state['status']:<12} {row['title']}; {state['review_label']}")
        return
    if 'plan' in data:
        plan, state = data['plan'], data['readiness']
        print(f"{plan['starter_id']}: {state['status']}; {state['review_label']}")
        print(f"{plan['kind']}: {plan['target']}; flow source: {plan['provenance']['flow_source']}")
        for name, binding in plan['bindings'].items():
            print(f"  {name}: {binding['runtime_id']} / {binding.get('model_id') or 'harness default'}")
        print(f"verification: {state['verification']}; limits: {json.dumps(plan['limits'])}")
        for diagnostic in state['diagnostics']:
            print(f"  {diagnostic['message']} — {diagnostic['fix']}")
        print(json.dumps({'sources': plan['sources'], 'checks': plan['checks'], 'options': plan['options']}, indent=2))
        print(plan['equivalent_command'])
    elif 'coverage' in data:
        state = data['state']
        print(f"session {data['session_id']}: process={state['process']}, work={state['work']}, outcome={state['outcome']}")
        print(f"review: {data.get('review_label', 'unknown')}; verification: {data['verification']['status']}")
        print(json.dumps({key: data.get(key) for key in ('goal', 'constraints', 'requirements', 'exclude', 'approved_scope', 'supplied_sources', 'output', 'acceptance_receipts', 'identities', 'artifacts', 'coverage')}, indent=2))
        action = data['next_action']
        print(f"Next: {action['label']}")
        if action.get('command'):
            print(action['command'])
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def run(args):
    service = StarterService()
    try:
        if args.starter_command == 'list':
            render(service.list(args.workspace), as_json=args.json)
        elif args.starter_command == 'show':
            entries = load_catalog()
            if args.starter_id not in entries:
                raise StarterError('starter.unknown', 'select an installed starter')
            state = next(row['readiness'] for row in service.list(args.workspace) if row['id'] == args.starter_id)
            render({**asdict(entries[args.starter_id]), 'readiness': state}, as_json=args.json)
        elif args.starter_command == 'result':
            render(service.result(args.session, workspace=args.workspace, limit=args.limit, offset=args.offset), as_json=args.json)
        elif args.starter_command == 'example':
            from garuda.scenarios.examples import materialize_reconnect

            render(materialize_reconnect(args.directory))
        elif args.starter_command == 'run':
            inputs = _inputs(args)
            if args.preview:
                render(service.preview(args.starter_id, inputs, args.workspace,
                                       allow_cross_project_context=args.allow_cross_project_context), as_json=args.json)
            else:
                plan = compile_scenario(args.starter_id, inputs, args.workspace, store=service.store,
                                        allow_cross_project_context=args.allow_cross_project_context)
                # Legacy runtime progress goes to stderr in this one-shot CLI;
                # stdout has one result document. No shared service redirects IO.
                with redirect_stdout(sys.stderr):
                    started = asyncio.run(service.start(plan))
                if started['session_id'] is None:
                    render({'error': {'code': 'starter.launch_refused',
                                      'message': 'The existing role executor refused before creating a session.'}}, as_json=args.json)
                    return started['exit_code'] or 2
                data = service.result(started['session_id'])
                render(data, as_json=args.json)
                return started['exit_code']
        return 0
    except (*RESOLUTION_ERRORS, ConfinementRefused, AcpError, TagError, ProjectIdentityError, FlowStopped, BackgroundRefused, ValueError, OSError) as exc:
        from garuda.context.redact import redact_text

        message = redact_text(str(exc))[0]
        if getattr(args, 'json', False):
            print(json.dumps({'error': {'code': getattr(exc, 'code', 'starter.invalid'), 'message': message}}))
        else:
            print(f'Error: {message}', file=sys.stderr)
        return 2
