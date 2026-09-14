"""One durable parent authorization journal for sequential platform trials.

This is an outer execution allowance, separate from native Provider accounting.
A reservation consumes an allowance before the child may register an attempt.
Crashes, missing evidence or incomplete cleanup keep that reservation unresolved;
opening another database or process cannot clear it. Only the coordinator may
own this local journal; workers receive no journal mutation capability.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Iterator
from .files import canonical,digest,read_json,write_new

MAX_ATTEMPTS=16
MIN_REQUEST_INTERVAL_NS=30_000_000_000
MAX_DIRECTORY_BYTES=12*1024**3
MIN_FREE_BYTES=4*1024**3
MAX_OPERATIONS=20000


@dataclass(frozen=True)
class Observation:
    """A current machine observation, not a model's assertion of budget safety."""
    directory_bytes: int
    free_bytes: int
    persistent_operations: int
    monitoring_current: bool

    def check(self) -> None:
        if (any(type(v) is not int or v<0 for v in (self.directory_bytes,self.free_bytes,self.persistent_operations))
            or self.monitoring_current is not True or self.directory_bytes>=MAX_DIRECTORY_BYTES
            or self.free_bytes<=MIN_FREE_BYTES or self.persistent_operations>=MAX_OPERATIONS):
            raise ValueError('Resource stop condition reached.')


def uncorrected_stops(entries: list[dict]) -> list[dict]:
    """Keep every real stop; only a proven local collection error has a correction."""
    corrected={e['token'] for e in entries if e['kind']=='LOCAL_OBSERVATION_CORRECTED'}
    return [e for e in entries if e['kind']=='SETTLED' and e['continue_allowed'] is not True and e['token'] not in corrected]


class AuthorizationJournal:
    """Exclusive append-only coordinator state; one unresolved child stops dispatch."""
    def __init__(self,path: Path):
        self.path=path

    @contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor=os.open(self.path/'coordinator.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield
        finally:os.close(descriptor)

    def create(self,approval: dict) -> None:
        """Persist one frozen explicitly approved allowance; existing state rejects."""
        required={'package_digest','code_digest','approved_by','approval_ref','cost_limit_atoms','quota_limit',
                  'per_attempt_money_bound','per_attempt_quota_bound','operations','extra_operations'}
        usage_only=approval.get('billing_policy')=='USAGE_ONLY_TRIAL'
        deepseek=approval.get('billing_policy')=='DEEPSEEK_TOKEN_METERED_TRIAL'
        if usage_only or deepseek:required.add('billing_policy')
        if deepseek:required.add('independent_trial')
        if set(approval)!=required or any(not approval[k] for k in ('approved_by','approval_ref')):
            raise ValueError('Explicit complete approval is required.')
        numeric=('cost_limit_atoms','quota_limit','per_attempt_money_bound','per_attempt_quota_bound')
        if any(type(approval[k]) is not int or not 0<=approval[k]<2**63 for k in numeric):
            raise ValueError('Exact nonnegative budget quantities required.')
        if usage_only:
            if any(approval[k]!=0 for k in numeric):raise ValueError('Usage-only policy has no monetary or quota claim.')
        elif approval['cost_limit_atoms']==0:raise ValueError('A positive authorization ceiling is required.')
        standard=tuple(f'{platform}:{operation}' for platform in ('macos','linux') for operation in ('persona',*(f'learn-{i}' for i in range(6))))
        if tuple(approval['operations'])!=standard or type(approval['extra_operations']) is not dict or len(approval['extra_operations'])>2:
            raise ValueError('A fixed two-platform allowance is required.')
        if set(standard)&set(approval['extra_operations']) or any(not purpose for purpose in approval['extra_operations'].values()):
            raise ValueError('Extras require distinct named purposes.')
        if deepseek:
            from .deepseek_authorization import validate_approval
            validate_approval(approval)
        self.path.mkdir(mode=0o700)
        with self._locked():write_new(self.path/'approval.json',canonical(approval))

    def _read(self) -> tuple[dict,list[dict]]:
        approval=read_json(self.path/'approval.json')
        if type(approval) is not dict:raise ValueError('Invalid authorization.')
        entries=[];previous=digest(approval)
        files=sorted(self.path.glob('event-*.json'))
        if len(files)>2*MAX_ATTEMPTS+32:raise ValueError('Journal capacity exceeded.')
        for ordinal,path in enumerate(files):
            if path.name!=f'event-{ordinal:03d}.json':raise ValueError('Missing authorization evidence.')
            entry=read_json(path,16384)
            if type(entry) is not dict or entry.get('previous')!=previous:raise ValueError('Authorization evidence mismatch.')
            previous=digest(entry);entries.append(entry)
        return approval,entries

    def _append(self,approval: dict,entries: list[dict],value: dict) -> None:
        write_new(self.path/f'event-{len(entries):03d}.json',canonical({'previous':digest(entries[-1] if entries else approval),**value}))

    def reserve(self,operation: str,package_digest: str,code_digest: str,observation: Observation) -> str:
        """Consume once before child launch; replay or any open reservation rejects."""
        observation.check()
        with self._locked():
            approval,entries=self._read()
            qualified=[e for e in entries if e['kind']=='IMPLEMENTATION_QUALIFIED']
            current_code=qualified[-1]['code_digest'] if qualified else approval['code_digest']
            if (package_digest,code_digest)!=(approval['package_digest'],current_code):
                raise ValueError('Frozen package or implementation changed.')
            reservations=[e for e in entries if e['kind']=='RESERVED'];settlements=[e for e in entries if e['kind']=='SETTLED']
            if len(reservations)!=len(settlements) or uncorrected_stops(entries):
                raise ValueError('Unresolved or stopped prior execution.')
            if settlements and time.time_ns()-settlements[-1]['settled_at_ns']<MIN_REQUEST_INTERVAL_NS:
                raise ValueError('Minimum request interval has not elapsed.')
            maximum=14 if approval.get('billing_policy')=='DEEPSEEK_TOKEN_METERED_TRIAL' else MAX_ATTEMPTS
            if len(reservations)>=maximum or operation in {e['operation'] for e in reservations}:
                raise ValueError('Attempt allowance exhausted or operation already consumed.')
            extras={e['operation']:e['purpose'] for e in entries if e['kind']=='EXTRA_APPROVED'}
            if operation not in (*approval['operations'],*approval['extra_operations'],*extras):raise ValueError('Operation is not approved.')
            total_money=sum(e['money_responsibility'] for e in settlements)+approval['per_attempt_money_bound']
            total_quota=sum(e['quota_responsibility'] for e in settlements)+approval['per_attempt_quota_bound']
            if total_money>approval['cost_limit_atoms'] or total_quota>approval['quota_limit']:
                raise ValueError('Shared authorization budget exhausted.')
            token=digest({'approval':digest(approval),'operation':operation,'ordinal':len(reservations)})
            self._append(approval,entries,{'kind':'RESERVED','operation':operation,'token':token,
                'money_responsibility':approval['per_attempt_money_bound'],'quota_responsibility':approval['per_attempt_quota_bound']})
            return token

    def settle(self,token: str,*,evidence_digest: str,attempts: int,money: int,quota: int,
               remote_known: bool,local_committed: bool,cleanup_ended: bool,cost_complete: bool,
               integrity_ok: bool,usage_complete: bool=False) -> bool:
        """Attach actual child evidence; UNKNOWN/held/faults irreversibly stop new work.

        Even a registered but unsent attempt consumes the original reservation.
        This journal does not release or overwrite native Provider liabilities.
        """
        with self._locked():
            approval,entries=self._read()
            if not entries or entries[-1]['kind']!='RESERVED' or entries[-1]['token']!=token:
                raise ValueError('No matching unresolved execution.')
            if (len(evidence_digest)!=64 or any(c not in '0123456789abcdef' for c in evidence_digest)
                or any(type(v) is not int or not 0<=v<2**63 for v in (attempts,money,quota))):
                raise ValueError('Exact bounded execution evidence required.')
            usage_only=approval.get('billing_policy')=='USAGE_ONLY_TRIAL'
            coverage=(usage_complete is True and cost_complete is False and money==quota==0) if usage_only else cost_complete
            allowed=(attempts<=1 and all(v is True for v in (remote_known,local_committed,cleanup_ended,coverage,integrity_ok))
                     and money<=entries[-1]['money_responsibility'] and quota<=entries[-1]['quota_responsibility'])
            # Preserve the original reservation whenever known coverage is absent.
            self._append(approval,entries,{'kind':'SETTLED','token':token,'evidence_digest':evidence_digest,
                'settled_at_ns':time.time_ns(),'attempts':attempts,'money_responsibility':money if cost_complete else max(money,entries[-1]['money_responsibility']),
                'quota_responsibility':quota if cost_complete else max(quota,entries[-1]['quota_responsibility']),
                'remote_known':remote_known,'local_committed':local_committed,'cleanup_ended':cleanup_ended,
                'cost_complete':cost_complete,'usage_complete':usage_complete,'integrity_ok':integrity_ok,'continue_allowed':allowed})
            return allowed

    def correct_local_observation(self, original: dict, recovered: dict, reconciliation: dict) -> None:
        """Append proof of a query-only collection error, retaining all liabilities.

        This cannot correct a real UNKNOWN, missing usage, failed persistence or
        unfinished cleanup. The trusted coordinator supplies unchanged artifacts
        whose digests were already bound into the original stopped settlement.
        No amount, attempt, user allowance or historical record is overwritten.
        """
        with self._locked():
            approval,entries=self._read();stops=uncorrected_stops(entries)
            reserved=[e for e in entries if e['kind']=='RESERVED'];settled=[e for e in entries if e['kind']=='SETTLED']
            if (approval.get('billing_policy')!='DEEPSEEK_TOKEN_METERED_TRIAL' or len(stops)!=1
                    or len(reserved)!=len(settled) or stops[0] is not settled[-1]):
                raise ValueError('Only the last local observation stop is eligible.')
            stop=stops[0]
            if (stop['attempts']!=1 or stop['local_committed'] is not True or stop['cleanup_ended'] is not True
                    or stop['integrity_ok'] is not False or stop['cost_complete'] is not False
                    or stop['evidence_digest']!=digest(reconciliation)
                    or reconciliation['original_result_digest']!=digest(original)
                    or reconciliation['recovery_result_digest']!=digest(recovered)):
                raise ValueError('Original immutable stop evidence mismatch.')
            if ('provider' in original or 'budget' in original or not original.get('queries')
                    or not any('RESOURCE_BUSY' in q['result'] and 'ADMISSION_FULL' in q['result'] for q in original['queries'] if type(q['result']) is str)):
                raise ValueError('A proven pre-accounting local query collection error is required.')
            for field in ('receipt','work','objects','sources','current_persona'):
                if original[field]!=recovered[field]:raise ValueError('Original committed binding changed.')
            for value in (original,recovered):
                if (value['local_terminal_confirmed'] is not True or value['close_report'] is not True
                        or value['monitor_failed'] or value['manifest_unchanged'] is not True):
                    raise ValueError('Unconfirmed local operation or monitor cannot be corrected.')
                for field in ('provider_health_after_close','storage_health_after_close','media_health_after_close'):
                    if value[field]['cleanup_pending'] is not False:raise ValueError('Cleanup remains pending.')
                health=value['provider_health_after_close'];storage=value['storage_health_after_close']
                if (health['lifecycle']!='CLOSED' or health['in_flight']!=0 or health['unknown_observations']!=0
                        or health['ledger_faulted'] is not False or storage['lifecycle']!='CLOSED'
                        or any(storage[k]!=0 for k in ('unresolved_operations','reads_in_flight','writes_in_flight'))):
                    raise ValueError('Unsettled native execution cannot be corrected.')
            provider=recovered['provider'];attempts=provider['attempts'];request=provider['request']
            if len(attempts)!=1:raise ValueError('Exactly the original native attempt is required.')
            attempt=attempts[0];usage=attempt['usage'];budget=recovered['budget'][0]
            if (request['object_id']!=original['observed_request_id'] or request['object_id']!=original['work']['provider_request_id']
                    or request['task_role']!='LEARNING' or request['phase']!='TERMINAL' or request['outcome']!='SUCCEEDED'
                    or request['ever_unknown'] is not False or attempt['ever_unknown'] is not False
                    or attempt['request_id']!=request['object_id'] or attempt['state']!='COMPLETED'
                    or attempt['first_error'] is not None or attempt['terminal_error'] is not None
                    or attempt['wire_protocol']!='DEEPSEEK_CHAT_JSON_V1' or request['account_id']!='deepseek-trial-account'
                    or usage['cost_complete'] is not True or usage['valid'] is not True or usage['coverage']!='COMPLETE'
                    or usage['held_atoms']!=0 or budget['held_atoms']!=0 or budget['risk_state']!='CLEAR'
                    or usage['estimated_cost_atoms']>stop['money_responsibility']
                    or len(recovered['queries'])!=len(recovered['objects'])
                    or any(type(q['result']) is not dict or q['result'].get('availability') not in ('COMPLETE','DEGRADED')
                        or not any(m['object_id']==q['object_id'] for m in q['result'].get('sections',{}).get('memories',[]))
                        for q in recovered['queries'])):
                raise ValueError('Actual unknown, cost, identity or query failure cannot be corrected.')
            self._append(approval,entries,{'kind':'LOCAL_OBSERVATION_CORRECTED','token':stop['token'],
                'original_stop_digest':digest(stop),'original_evidence_digest':stop['evidence_digest'],
                'recovered_result_digest':digest(recovered),'request_id':request['object_id'],'attempt_id':attempt['object_id'],
                'actual_remote_known':True,'actual_cost_complete':True,'actual_native_held_atoms':0,
                'observed_estimated_cost_atoms':usage['estimated_cost_atoms'],'retained_money_responsibility':stop['money_responsibility'],
                'corrected_at_ns':time.time_ns(),'basis':'IMMUTABLE_NATIVE_RECOVERY_OF_LOCAL_QUERY_COLLECTION_ERROR'})

    def qualify_implementation(self,code_digest: str,evidence_digest: str) -> None:
        """Append an in-scope repair qualification without resetting any allowance.

        The coordinator supplies immutable offline check evidence. This changes
        only the admitted implementation fingerprint, never materials, operation
        purposes, unresolved reservations or previous stop decisions.
        """
        if any(type(value) is not str or len(value)!=64 or any(c not in '0123456789abcdef' for c in value)
               for value in (code_digest,evidence_digest)):raise ValueError('Qualification digests required.')
        with self._locked():
            approval,entries=self._read()
            if sum(e['kind']=='RESERVED' for e in entries)!=sum(e['kind']=='SETTLED' for e in entries):
                raise ValueError('Unresolved execution cannot change implementation.')
            if sum(e['kind']=='IMPLEMENTATION_QUALIFIED' for e in entries)>=32:raise ValueError('Qualification capacity exceeded.')
            self._append(approval,entries,{'kind':'IMPLEMENTATION_QUALIFIED','code_digest':code_digest,'evidence_digest':evidence_digest})

    def approve_extra(self,operation: str,purpose: str,approval_ref: str) -> None:
        """Record a user's specific extra purpose; callers must hold that approval.

        This does not consume, replay, reset or unblock a request. The same
        immutable journal retains both initial allowance and later user intent.
        """
        if any(type(value) is not str or not value.strip() or len(value.encode())>1024 for value in (operation,purpose,approval_ref)):
            raise ValueError('Explicit specific extra approval required.')
        with self._locked():
            approval,entries=self._read()
            if approval.get('billing_policy')=='DEEPSEEK_TOKEN_METERED_TRIAL':raise ValueError('This allowance has no spare or retry slots.')
            extras={e['operation'] for e in entries if e['kind']=='EXTRA_APPROVED'}|set(approval['extra_operations'])
            if len(extras)>=2 or operation in (*approval['operations'],*extras):raise ValueError('Extra allowance exhausted or duplicate.')
            self._append(approval,entries,{'kind':'EXTRA_APPROVED','operation':operation,'purpose':purpose,'approval_ref':approval_ref})

    def read_snapshot(self) -> dict:
        """Read a parent's immutable child-launch snapshot without mutation.

        The coordinator must remain parked while its single child runs. This
        reader checks the hash chain but cannot reserve or settle an attempt.
        """
        approval,entries=self._read()
        return {'approval':approval,'events':entries}

    def inspect(self) -> dict:
        """Return nonsecret preserved allowance facts without dispatch or reset."""
        with self._locked():
            approval,entries=self._read()
            return {'approval':approval,'events':entries}
