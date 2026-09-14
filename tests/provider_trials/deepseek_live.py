"""Activated DeepSeek persona worker bound to one shared coordinator reservation.

The parent alone reserves and settles. This worker checks the unchanged package,
implementation, independent approval and current journal chain before native
initialization or credential capability. Seed/preflight/recover never read a key;
only the native Provider resolves the retained protected reference for a send.
"""
import copy
import argparse
import asyncio
import os
from pathlib import Path
import sys
from typing import cast
from companion_memory.provider.file_credentials import InheritedFileCredential
from .authorization import AuthorizationJournal,uncorrected_stops
from .deepseek_authorization import validate_approval
from .files import read_json,digest
from .live_worker import execute,manifest


def admission(package: dict, snapshot: dict, root: Path, platform: str, action: str, permit: dict|None, ordinal: int|None=None) -> None:
    """Reject unapproved or replayed launches without reading credentials."""
    approval=snapshot['approval'];validate_approval(approval)
    if action not in ('seed','preflight','persona','recover','publish','learn','recover-learning'):raise ValueError('Unsupported DeepSeek trial action.')
    qualified=[e for e in snapshot['events'] if e['kind']=='IMPLEMENTATION_QUALIFIED']
    code=qualified[-1]['code_digest'] if qualified else approval['code_digest']
    if action in ('learn','recover-learning') and (type(ordinal) is not int or not 0<=ordinal<6):raise ValueError('Original batch ordinal required.')
    if (approval['package_digest']!=digest(package) or code!=manifest()[1]
            or approval['approved_by']!='用户' and approval['approved_by']!='synthetic-human'
            or not approval['approval_ref'] or not root.is_absolute()
            or str(root)!=approval['independent_trial']['new_roots'][platform]):
        raise ValueError('Activated package, implementation or isolated root mismatch.')
    if package['activation_template']['independent_trial']['new_roots']!=approval['independent_trial']['new_roots']:
        raise ValueError('Approved roots changed from the frozen proposal.')
    expected=copy.deepcopy(package['activation_template'])
    for key in ('package_digest','code_digest','approved_by','approval_ref'):expected[key]=approval[key]
    for key in ('count_approved','money_approved','independent_under_old_unknown_approved','user_decision_ref'):
        expected['independent_trial'][key]=approval['independent_trial'][key]
    if expected!=approval:raise ValueError('Activation changed frozen limits or old stop evidence.')
    if action in ('persona','learn'):
        events=snapshot['events'];operation=platform+':'+('persona' if action=='persona' else 'learn-'+str(ordinal))
        if (not events or events[-1]['kind']!='RESERVED' or events[-1]['operation']!=operation
                or permit is None or set(permit)!={'operation','code_digest','package_digest','token','root','platform'}
                or permit!={'operation':operation,'code_digest':code,'package_digest':approval['package_digest'],
                    'token':events[-1]['token'],'root':str(root),'platform':platform}):
            raise ValueError('An original unsettled reservation must precede native dispatch.')
        if uncorrected_stops(events):
            raise ValueError('A prior execution stopped this shared allowance.')


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('seed','preflight','persona','recover','publish','learn','recover-learning'))
    for name in ('root','output','package','journal'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--platform',choices=('macos','linux'),required=True)
    parser.add_argument('--ordinal',type=int);parser.add_argument('--user-approval',type=Path)
    parser.add_argument('--permit',type=Path);parser.add_argument('--credential',type=Path)
    parser.add_argument('--drop-linux-privileges',action='store_true');args=parser.parse_args()
    package=cast(dict,read_json(args.package));snapshot=AuthorizationJournal(args.journal).read_snapshot()
    permit=None if args.permit is None else cast(dict,read_json(args.permit))
    admission(package,snapshot,args.root,args.platform,args.action,permit,args.ordinal)
    user_approval=None if args.user_approval is None else cast(dict,read_json(args.user_approval))
    if args.action=='publish' and (user_approval is None or user_approval['package_digest']!=digest(package)):
        raise ValueError('User review must bind the original package.')
    binding=None
    # Public authorization is read before UID drop because the mounted snapshot
    # is private. The protected descriptor constructor reads metadata only.
    if args.drop_linux_privileges:
        if sys.platform!='linux' or os.geteuid()!=0 or args.platform!='linux':raise ValueError('Invalid privilege drop.')
        if args.action in ('preflight','persona','learn'):
            if args.credential!=Path('/credential/llm.api-key'):raise ValueError('Fixed protected credential mount required.')
            binding=InheritedFileCredential(args.credential,secret_ref='deepseek-trial-secret',
                secret_revision='deepseek-trial-secret-revision',account_ref='deepseek-trial-account',recipient_uid=65534)
        os.setgroups([]);os.setgid(65534);os.setuid(65534)
    try:
        # Retain the validated nonsecret permit across the irreversible UID drop.
        result=asyncio.run(execute(args.root,args.platform,args.action,args.output,args.permit,args.credential,
            binding.resolver if binding is not None else None,supplier='deepseek',frozen_package=package,validated_permit=permit,user_approval=user_approval,ordinal=args.ordinal))
    finally:
        if binding is not None:binding.close()
    import json
    print(json.dumps({'action':args.action,'platform':args.platform,'close_report':result['close_report'],
        'monitor_failed':result['monitor_failed'],'manifest_unchanged':result['manifest_unchanged']}))

if __name__=='__main__':main()
