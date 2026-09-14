"""Single-operation native trial worker with explicit frozen authorization.

Seed and recover have no credential capability or network access. Send requires
the parent's immutable reservation, uses one native persona operation and records
separate remote, persistence, usage and actual cleanup observations. There is no
automatic retry, review, publication or next-operation loop.
"""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict,is_dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sqlite3
import ssl
import sys
import time
from typing import cast
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.text_records import stable_identity
from companion_memory.provider import ObserverGrant,Found as ProviderFound
from companion_memory.provider.credentials import CredentialResolver,CredentialUnavailable
from companion_memory.provider.file_credentials import protected_file_resolver,InheritedFileCredential
from companion_memory.provider.wire_evidence import WireEvidence
from companion_memory.provider.values import dump
from companion_memory.ingress.events import plain
from tests.text_learning.host_driver import confirm_local
from .authorization import Observation
from .files import canonical,digest,read_json,write_new
from .host import assemble
from .minimax_configuration import configuration
from .persona_execution import prepare,pending,join


def manifest() -> tuple[list[dict],str]:
    """Hash only maintained nonsecret code, tests, resources and engineering files."""
    names=[p for root in ('companion_memory','tests') for p in Path(root).rglob('*')
           if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc']
    names += [Path(name) for name in ('pyproject.toml','uv.lock','.python-version','.vscode/settings.json','.gitignore') if Path(name).is_file()]
    entries=[{'path':p.as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(names)]
    aggregate=b''.join((e['path']+'\0'+e['sha256']+'\n').encode() for e in entries)
    return entries,hashlib.sha256(aggregate).hexdigest()


def observe(root: Path) -> Observation:
    """Inspect real filesystem and committed operation count for qualification."""
    total=0
    for directory,_,files in os.walk(root):
        for name in files:
            path=Path(directory)/name
            try:
                if not path.is_symlink():total+=path.stat().st_size
            except FileNotFoundError:pass  # Atomic owner cleanup may remove a leaf.
    database=root/'database/runtime.sqlite3';operations=0
    if database.exists():
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=.5) as db:
            operations=db.execute('SELECT count(*) FROM operation_receipts').fetchone()[0]
    result=Observation(total,shutil.disk_usage(root).free,operations,True);result.check()
    return result


def controlled_network(send: bool, hostname: str = 'api.minimax.cn') -> dict:
    """Restrict this supported worker's socket audit boundary to official HTTPS.

    This is a process-level guard, not a kernel sandbox for malicious Python.
    DNS and TLS verification do not issue an HTTP request or resolve credentials.
    """
    if hostname not in ('api.minimax.cn','api.deepseek.com'):raise ValueError('Unsupported trial origin.')
    if os.geteuid()==0:raise ValueError('A nonroot trial worker is required.')
    addresses=socket.getaddrinfo(hostname,443,type=socket.SOCK_STREAM) if send else []
    allowed={str(row[4][0]) for row in addresses}
    def audit(event: str,args: tuple) -> None:
        if event=='socket.getaddrinfo' and (not send or args[0]!=hostname or args[1]!=443):
            raise PermissionError('Trial DNS destination denied.')
        if event=='socket.connect':
            address=args[1]
            if not send or type(address) is not tuple or address[0] not in allowed or address[1]!=443:
                raise PermissionError('Trial network destination denied.')
    sys.addaudithook(audit)
    context=ssl.create_default_context()
    result={'platform':platform.platform(),'machine':platform.machine(),'uid':os.geteuid(),'python':platform.python_version(),
        'sqlite':sqlite3.sqlite_version,'openssl':ssl.OPENSSL_VERSION,'ca':context.cert_store_stats(),
        'allowed_origin':hostname+':443' if send else None,'network_guard':'PROCESS_AUDIT_FIXED_DESTINATION',
        'tls_verified':False,'probe_http_bytes':0}
    if send:
        with socket.create_connection((hostname,443),timeout=10) as connection:
            with context.wrap_socket(connection,server_hostname=hostname) as tls:
                result.update(tls_verified=True,tls_version=tls.version())
    if Path('/proc/mounts').exists():
        result['persistent_mount']=[line for line in Path('/proc/mounts').read_text().splitlines() if line.split()[1]=='/persistent']
        result['limits']={name:Path('/sys/fs/cgroup',name).read_text().strip() for name in ('cpu.max','memory.max','memory.swap.max','pids.max')}
    return result


async def execute(root: Path,platform_name: str,action: str,output: Path,permit: Path|None,credential: Path|None,
                  inherited: CredentialResolver|None=None, *, supplier: str='minimax', frozen_package: dict|None=None, validated_permit: dict|None=None,
                  user_approval: dict|None=None, ordinal: int|None=None) -> dict:
    """Seed, send once, or locally recover the same platform database."""
    if supplier not in ('minimax','deepseek'):raise ValueError('Unknown trial supplier.')
    deepseek=supplier=='deepseek'
    if deepseek and (action=='persona-retry' or frozen_package is None):raise ValueError('Frozen DeepSeek package with no retry required.')
    account_ref='deepseek-trial-account' if deepseek else 'minimax-trial-account'
    secret_ref='deepseek-trial-secret' if deepseek else 'trial-llm-secret'
    secret_revision='deepseek-trial-secret-revision' if deepseek else 'trial-llm-revision'
    send=action in ('persona','persona-retry','learn')
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    _,code=manifest();authorization=None
    if send:
        if permit is None or credential is None:raise ValueError('An original reservation and protected credential reference are required.')
        authorization=validated_permit if validated_permit is not None else cast(dict,read_json(permit))
        if (set(authorization)!={'operation','code_digest','package_digest','token','root','platform'}
            or authorization['operation']!=platform_name+':'+('learn-'+str(ordinal) if action=='learn' else action) or authorization['code_digest']!=code
            or authorization['root']!=str(root) or authorization['platform']!=platform_name):raise ValueError('Frozen send authorization mismatch.')
    environment=controlled_network(send or action=='preflight','api.deepseek.com' if deepseek else 'api.minimax.cn');write_new(output/'environment.json',canonical(environment))
    if action=='seed':root.mkdir(mode=0o700,parents=True,exist_ok=False)
    if deepseek:
        from .deepseek_configuration import configuration as selected_configuration
        from companion_memory.configuration.text_codec import candidate_values
        config,inputs=selected_configuration(root)
        assert frozen_package is not None
        if candidate_values(config)!=frozen_package['configurations'][platform_name]['native_domains']:
            raise ValueError('Actual complete configuration differs from frozen package.')
    else:config,inputs=configuration(root)
    resolver=CredentialResolver(lambda *args:CredentialUnavailable('UNAVAILABLE'))
    if send:
        assert credential is not None
        resolver=inherited if inherited is not None else protected_file_resolver(credential,secret_ref=secret_ref,secret_revision=secret_revision,account_ref=account_ref)
    evidence=WireEvidence(output) if send else None
    host=assemble(config,root,platform_name,inputs[6],resolver,evidence=evidence)
    result={'action':action,'platform':platform_name,'code_digest':code,'authorization':authorization,'started_ns':time.time_ns()}
    monitor_stop=asyncio.Event();samples=[];monitor_failed=[]
    async def monitor():
        while not monitor_stop.is_set():
            try:
                observation=await asyncio.to_thread(observe,root)
                samples.append({'at_ns':time.time_ns(),**asdict(observation)})
            except (OSError,ValueError,sqlite3.Error):monitor_failed.append('RESOURCE_OBSERVATION_FAILED');return
            try:await asyncio.wait_for(monitor_stop.wait(),timeout=.5)
            except TimeoutError:pass
    task=None
    try:
        opened=await confirm_local(lambda:host.initialize('CREATE_NEW' if action=='seed' else 'OPEN_EXISTING'))
        result['opened']=repr(opened)
        if type(opened) is not Found:raise ValueError('Native host initialization did not complete.')
        assert host.stored is not None
        result['database_id']=host.stored.database_id;result['snapshot_id']=host.stored.snapshot_id
        task=asyncio.create_task(monitor())
        result['before']=asdict(await asyncio.to_thread(observe,root))
        run_id=stable_identity('persona-run',host.stored.database_id,host.resources.instance_id)
        if action=='seed':
            run_id,key=await prepare(host);result['provider_operation_key']=key
        elif action=='publish':
            from .live_operations import publish
            if user_approval is None:raise ValueError('Explicit original candidate approval required.')
            await publish(host,run_id,user_approval,platform_name,result)
        elif action in ('learn','recover-learning'):
            from .live_operations import learn
            if monitor_failed:raise ValueError('Current resource monitor unavailable.')
            await learn(host,cast(int,ordinal),send,result)
        elif send:
            owner=host.combination.persona.persona;assert owner is not None
            run=await owner.read_original('run',run_id,time.monotonic()+10)
            generation=1 if action=='persona' else 2
            if action=='persona-retry':
                if run is None or run.value['state']!='KNOWN_FAILED' or run.value['generation']!=1:
                    raise ValueError('The original known failed first generation is required.')
                original_run=run.value
                retried=await confirm_local(lambda:host.initialization_port().retry_initial_persona('retry-persona-2',run_id,
                    cast(int,original_run['revision']),1,cast(str,original_run['resolution_id']),host.gate.epoch,time.monotonic()+10))
                await join(host);result['retry_receipt']=repr(retried)
                if type(retried) is not Committed:raise ValueError('The original retry was not committed.')
                run=await owner.read_original('run',run_id,time.monotonic()+10)
            if run is None or run.value['state']!='PREPARED' or run.value['generation']!=generation:
                raise ValueError('The original unconsumed persona run is required.')
            if run.value['publication_id'] is not None:raise ValueError('Published persona cannot generate again.')
            if monitor_failed:raise ValueError('Current resource monitor unavailable.')
            result['provider_operation_key']=str(run.value['provider_operation_key'])
            generated=await host.initialization_port().generate(run_id,generation,str(run.value['provider_operation_key']),time.monotonic()+60)
            result['generated']=repr(generated)
            await join(host)
        result['pending']=await pending(host,run_id) if action!='seed' else None
        owner=host.combination.persona.persona;assert owner is not None
        run=await owner.read_original('run',run_id,time.monotonic()+10)
        result['run']=None if run is None else plain(run.value)
        observed_request=result.get('observed_request_id',None if run is None else run.value['provider_request_id'])
        if observed_request is not None:
            observer=host.provider.bind_observer(ObserverGrant((host.resources.instance_id,),(account_ref,)))
            request=await observer.get_request(observed_request)
            result['provider']=json.loads(dump(request.value)) if type(request) is ProviderFound else {'error':repr(request)}
            budget=await observer.get_budget_state()
            result['budget']=json.loads(dump(budget.value)) if type(budget) is ProviderFound else {'error':repr(budget)}
            host.provider.revoke(observer)
        if run is not None and run.value['publication_id'] is not None:
            from .live_operations import current_persona
            await current_persona(host,result)
        result['provider_health_before_close']=asdict(host.provider.get_health())
        result['storage_health_before_close']=asdict(host.storage.get_health())
    finally:
        result['close_report']=await host.close()
        result['provider_health_after_close']=asdict(host.provider.get_health())
        result['storage_health_after_close']=asdict(host.storage.get_health())
        result['media_health_after_close']=asdict(host.media.get_health())
        monitor_stop.set()
        if task is not None:await task
        result['monitor_failed']=monitor_failed;result['after']=asdict(await asyncio.to_thread(observe,root))
        result['ended_ns']=time.time_ns();result['manifest_unchanged']=manifest()[1]==code
        write_new(output/'monitor.json',canonical(samples));write_new(output/'result.json',canonical(result))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('seed','persona','persona-retry','recover'));parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--platform',required=True,choices=('macos','linux'));parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--permit',type=Path);parser.add_argument('--credential',type=Path)
    parser.add_argument('--drop-linux-privileges',action='store_true')
    args=parser.parse_args()
    binding=None
    if args.drop_linux_privileges:
        if sys.platform!='linux' or os.geteuid()!=0 or args.platform!='linux':raise ValueError('Invalid privilege-drop environment.')
        if args.action in ('persona','persona-retry'):
            if args.credential!=Path('/credential/llm.api-key'):raise ValueError('Fixed mounted credential required.')
            binding=InheritedFileCredential(args.credential,secret_ref='trial-llm-secret',secret_revision='trial-llm-revision',account_ref='minimax-trial-account',recipient_uid=65534)
        os.setgroups([]);os.setgid(65534);os.setuid(65534)
    try:
        result=asyncio.run(execute(args.root,args.platform,args.action,args.output,args.permit,args.credential,
            binding.resolver if binding is not None else None))
    finally:
        if binding is not None:binding.close()
    print(json.dumps({'action':args.action,'platform':args.platform,'close_report':result['close_report'],
        'monitor_failed':result['monitor_failed'],'manifest_unchanged':result['manifest_unchanged']},ensure_ascii=False))


if __name__=='__main__':main()
