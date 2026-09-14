"""Freeze a nonsecret DeepSeek proposal without activating a sending allowance.

The old stop is bound as immutable evidence, never cleared. Complete resolved
configuration entries, resource bytes and material windows are exported. Actual
learning wire IDs and the human-approved new persona will be frozen by native
context at execution; qualification wires are explicitly synthetic examples.
"""
import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from companion_memory.configuration.text_codec import candidate_values
from companion_memory.configuration.content_codec import decode_content_entry, entries_digest
from companion_memory.provider.values import plain, freeze
from companion_memory.cognition.text_resources import output_schema, prompt_resource, business_instructions
from companion_memory.provider.token_costs import reserve_tokens, TokenPrices
from .deepseek_configuration import configuration, PRICE_SOURCE, PRICE_CHECKED, INPUT_BOUND
from .files import canonical, digest, write_new
from .materials import corpus


def proposal(old_evidence: Path, new_roots: dict[str,str]) -> dict:
    """Read only named nonsecret old evidence and return an inactive proposal."""
    names=('continuation-report.md','continuation-requests.json','macos-learning-5-unknown-recovery/result.json',
        'macos-learning-5-reconciliation.json','continuation-authorization-snapshot/approval.json')
    old={name:hashlib.sha256((old_evidence/name).read_bytes()).hexdigest() for name in names}
    events=[]
    for file in sorted((old_evidence/'continuation-authorization-snapshot').glob('event-*.json')):
        old[str(file.relative_to(old_evidence))]=hashlib.sha256(file.read_bytes()).hexdigest()
        events.append(json.loads(file.read_text()))
    reserved=[e for e in events if e['kind']=='RESERVED'];settled=[e for e in events if e['kind']=='SETTLED']
    assert len(reserved)==len(settled)==10 and settled[-1]['continue_allowed'] is False
    assert settled[-1]['remote_known'] is False and settled[-1]['cleanup_ended'] is True
    original=json.loads((old_evidence/'continuation-authorization-snapshot/approval.json').read_text())
    bound=reserve_tokens(INPUT_BOUND,2048,TokenPrices(2000000,40000,8000000))
    configurations={}
    for platform,root in new_roots.items():
        with TemporaryDirectory() as temporary:
            temporary_root=Path(temporary).resolve()
            value,_=configuration(temporary_root);encoded=candidate_values(value)
            # Relocation is proposal data only; the real platform resolves its
            # exact supplied paths again before native persistence or dispatch.
            encoded=json.loads(json.dumps(encoded).replace(str(temporary_root),root))
            for domain in encoded['domains']:
                domain['digest']=entries_digest(tuple((entry['parameter_key'],entry['body']) for entry in domain['entries']))
        entries=[e for d in encoded['domains'] for e in d['entries']]
        configurations[platform]={'native_domains':encoded,'values':{e['parameter_key']:plain(freeze(decode_content_entry(e['body'])[2],8192)) for e in entries},
            'entry_count':len(entries),'entry_bytes':sum(len(e['body'].encode()) for e in entries),'maximum_entry_bytes':max(len(e['body'].encode()) for e in entries)}
        assert len(entries)==118 and configurations[platform]['entry_bytes']<=524288 and configurations[platform]['maximum_entry_bytes']<=8192
    resources={}
    for role in ('LEARNING','PERSONA'):
        prompt=prompt_resource(role,'deepseek-flash');schema=output_schema(role)
        resources[role]={'prompt':prompt.decode(),'prompt_digest':hashlib.sha256(prompt).hexdigest(),
            'business_instructions':business_instructions(role,'deepseek-flash'),'schema':schema.decode(),'schema_digest':hashlib.sha256(schema).hexdigest()}
    approval={'package_digest':None,'code_digest':None,'approved_by':None,'approval_ref':None,
        'billing_policy':'DEEPSEEK_TOKEN_METERED_TRIAL','cost_limit_atoms':30000000,'quota_limit':0,
        'per_attempt_money_bound':bound,'per_attempt_quota_bound':0,
        'operations':[f'{p}:{o}' for p in ('macos','linux') for o in ('persona',*(f'learn-{i}' for i in range(6)))],
        'extra_operations':{},'independent_trial':{'new_attempts':14,'cost_limit_atoms':30000000,'old_attempts':10,
            'historical_maximum':24,'old_remaining_slots_transferable':False,'old_remote_state':'REMOTE_RESULT_UNKNOWN',
            'old_cleanup_ended':True,'old_stop_evidence_digest':digest(old),'old_package_digest':original['package_digest'],
            'new_account_id':'deepseek-trial-account','new_instances':{p:'deepseek-trial-'+p for p in new_roots},
            'new_database_ids':{p:'deepseek-text-trial-'+p for p in new_roots},'new_roots':new_roots,
            'count_approved':None,'money_approved':None,'independent_under_old_unknown_approved':None,'user_decision_ref':None}}
    return {'status':'PROPOSAL_NO_SEND_PERMISSION','authorization':None,'activation_template':approval,
        'old_stop_evidence':old,'configurations':configurations,'resources':resources,'materials':corpus(),
        'budget':{'source_url':PRICE_SOURCE,'checked_date':PRICE_CHECKED,'currency':'CNY','atom_scale':1000000,
            'price_policy':'PEAK_RATE_CONSERVATIVE_ESTIMATE_NOT_SUPPLIER_BILL','input_bound':INPUT_BOUND,'output_bound':2048,
            'input_atoms_per_million':2000000,'cached_atoms_per_million':40000,'output_atoms_per_million':8000000,
            'per_attempt_atoms':bound,'maximum_new_attempts':14,'aggregate_atoms':bound*14,'ceiling_atoms':30000000,
            'headroom_atoms':30000000-bound*14,'actual_supplier_bill_atoms':None},
        'quality_thresholds':{'precision':.90,'recall':.80,'anchor_support':1.0,'permission_violations':0},
        'execution_constraints':{'minimum_seconds_after_actual_cleanup':30,'automatic_retries':0,'spare_attempts':0,
            'new_persona_requires_own_human_approval':True,'runtime_context_freezes_actual_request_before_send':True}}


def prepare(output: Path, old_evidence: Path, new_roots: dict[str,str]) -> str:
    """Write only a fresh proposal directory; no authorization journal is created."""
    value=proposal(old_evidence,new_roots);checksum=digest(value)
    output.mkdir(mode=0o700)
    write_new(output/'package.json',canonical(value));write_new(output/'package.sha256',(checksum+'\n').encode())
    return checksum


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--old-evidence',type=Path,required=True);parser.add_argument('--macos-root',required=True);parser.add_argument('--linux-root',required=True)
    args=parser.parse_args();print(prepare(args.output,args.old_evidence,{'macos':args.macos_root,'linux':args.linux_root}))

if __name__=='__main__':main()
