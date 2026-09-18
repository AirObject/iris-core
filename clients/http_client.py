"""Executable explicit HTTP operation and original-confirmation client.

Examples:
  python -m clients.http_client --origin https://core.example --operation capabilities
  python -m clients.http_client --origin https://core.example --entry entry --operation accept --input clients/examples/event.json
  python -m clients.http_client --origin https://core.example --entry entry --operation query --input clients/examples/query.json
  python -m clients.http_client --origin https://core.example --entry entry --operation upload --key original-upload --file image.png

IRIS_HOST_TOKEN is supplied by the operator, never included in request journals
or URLs. Inputs contain their original keys. Repeat only with --confirm after an
uncertain response; a confirmation is printed without issuing replacement work.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from typing import Any
if __package__:
    from .iris_client import IrisClient
else:
    from iris_client import IrisClient

OPERATIONS = {
    'capabilities': ('capabilities', None), 'accept': ('accept', 'accept/resolve'),
    'query': ('memory/search', 'recalls/resolve'), 'prepare': ('prepare', 'recalls/resolve'),
    'deep-recall': ('memory/deep-recall', 'recalls/resolve'),
    'feedback': ('usage', 'usage/resolve'), 'state': ('state', None), 'goals': ('goals', None),
    'goal-inject': ('goals/inject', 'operations/resolve'), 'goal-status': ('goals/status', 'operations/resolve'),
    'goal-deadline': ('goals/deadline', 'operations/resolve'),
    'state-set': ('state/set', 'operations/resolve'), 'state-update': ('state/update', 'operations/resolve'),
    'state-end': ('state/end', 'operations/resolve'), 'media-inspect': ('media/inspect', None),
    'media-resolve': ('media/resolve', None), 'media-finish': ('media/finish', None),
    'notification-results': ('notifications/results', None),
}
NATIVE = {'goal-inject': 'inject_goal', 'goal-status': 'update_goal_status', 'goal-deadline': 'change_deadline',
    'state-set': 'set_state', 'state-update': 'update_state', 'state-end': 'end_activity'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--origin', required=True);parser.add_argument('--entry')
    parser.add_argument('--operation', choices=(*OPERATIONS, 'upload'), required=True)
    parser.add_argument('--input', type=Path);parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--file', type=Path);parser.add_argument('--key')
    parser.add_argument('--route');parser.add_argument('--after', default='', help='Last plan ID of the previous bounded result page.')
    parser.add_argument('--modality', choices=('IMAGE', 'AUDIO', 'VIDEO'), default='IMAGE')
    parser.add_argument('--ca-file');parser.add_argument('--token-env', default='IRIS_HOST_TOKEN')
    args = parser.parse_args()
    client = IrisClient(args.origin, os.environ[args.token_env], ca_file=args.ca_file)
    if args.operation not in ('capabilities', 'notification-results') and not args.entry: parser.error('--entry is required for scoped operations')
    if args.operation == 'upload':
        if not args.key: parser.error('--key is required and must survive uncertain responses')
        if args.confirm:
            result = client.media('resolve', args.entry, {'key': args.key, 'modality': args.modality})
        else:
            if args.file is None: parser.error('--file is required')
            result = client.upload(args.entry, args.key, args.file, args.modality)
    else:
        path, confirmation = OPERATIONS[args.operation]
        value: dict[str, Any] = json.loads(args.input.read_text()) if args.input else {}
        if args.confirm:
            if confirmation is None: parser.error('This observation has no original confirmation operation')
            path = confirmation
            if confirmation == 'operations/resolve': value = {'operation': NATIVE[args.operation], 'input': value}
            elif confirmation == 'recalls/resolve': value = {'query': value, 'prepared': args.operation == 'prepare', 'deep': args.operation == 'deep-recall'}
        if args.operation == 'notification-results':
            if not args.route: parser.error('--route is required')
            payload = {'route_id': args.route, 'after': args.after}
        else:
            payload = {} if args.operation == 'capabilities' else {'entry_id': args.entry, 'input': value}
        result = client.request('/api/host/' + path, payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
