"""Freeze nonsecret daily trial inputs without issuing any send capability.

The exact earlier review supplies existing memories. Dynamic documents remain
unbound until actual learning receipts exist; empty output never creates a
replacement. This preparation format is deliberately not an activation grant.
"""
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.daily_resolution import DailyConfigurationCandidate,daily_snapshot_issue
from companion_memory.configuration.daily_codec import candidate_values
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue
from .daily_trial_materials import write_materials,QUERIES,GOALS,SLOTS

ORIGINAL_DIGESTS={
    'proposal.canonical.json':'e93241a2235b56b99774cc4bd61d4cfa7948ac71649cd4648369d592a0a7df2f',
    'material-package.json':'0ef0c4dbc7c8b8ffbc50fd9e2270f663138f674079046c2ff57f8476fd03796f',
    'material-review-decision.json':'2643f1a824b6454213cabdf5bcef34fea50b373998e5687dc5a6cfb2f4394a81'}


def canonical(value) -> bytes:
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def approved_originals(directory:Path) -> dict:
    """Read only the exact approved evidence; never reinterpret earlier attempts."""
    values={}
    for name,digest in ORIGINAL_DIGESTS.items():
        raw=(directory/name).read_bytes()
        if len(raw)>1048576 or sha256(raw).hexdigest()!=digest:raise InvalidValue()
        values[name]=json.loads(raw)
    proposal=values['proposal.canonical.json'];package=values['material-package.json'];review=values['material-review-decision.json']
    if (review['expected_amended_proposal_sha256']!=ORIGINAL_DIGESTS['proposal.canonical.json']
            or package['proposal_sha256']!=ORIGINAL_DIGESTS['proposal.canonical.json']
            or len(proposal['memories'])!=12):raise InvalidValue()
    first=[]
    for ordinal,member in enumerate(proposal['memories'][:4]):
        rendered=package['requests'][ordinal]
        if rendered['purpose']!='DOCUMENT' or rendered['member_id']!=member['member_id']:raise InvalidValue()
        if sha256(rendered['rendered_text'].encode()).hexdigest()!=rendered['material_digest']:raise InvalidValue()
        if sha256(canonical([member['event_json'],member['memory_json']])).hexdigest()!=member['content_digest']:raise InvalidValue()
        first.append({'ordinal':ordinal,'member_id':member['member_id'],'event_json':member['event_json'],
            'memory_json':member['memory_json'],'content_digest':member['content_digest'],
            'rendered_text':rendered['rendered_text'],'material_digest':rendered['material_digest'],'origin':'FIXED_REVIEWED'})
    return {'evidence_directory':str(directory),'evidence_digests':dict(ORIGINAL_DIGESTS),'manifest_digest':package['manifest_digest'],
        'review_ref':review['review_ref'],'review_digest':ORIGINAL_DIGESTS['proposal.canonical.json'],
        'establish_ordinals':[0,1,2,3],'existing_memories':first,'old_attempts_available':0}


def purpose_slots() -> list[dict]:
    """Enumerate the closed 32 purposes; a slot cannot be reassigned."""
    result=[]
    def add(role,selector):
        result.append({'slot_id':role.lower()+':'+str(sum(s['role']==role for s in result)),
            'role':role,'selector':selector,'state':'UNUSED','original_request':None,'attempt':None,'usage':None,
            'known_cost_atoms':None,'actual_charge':None})
    for scene in ('A','B','C','D'):add('MEDIA',{'image_scene':scene})
    for entry in ('display-real','story-fiction'):
        for batch in range(2):
            for turn in range(3):add('LEARNING',{'entry_id':entry,'batch_ordinal':batch,'turn_ordinal':turn})
    for number,pair in enumerate(GOALS):add('GOAL_DEDUP',{'comparison_ordinal':number,'contents':pair})
    for ordinal in range(4):add('EMBEDDING_DOCUMENT',{'origin':'FIXED_REVIEWED','ordinal':ordinal})
    for entry in ('display-real','story-fiction'):
        for batch in range(2):
            for ordinal in range(2):add('EMBEDDING_DOCUMENT',{'origin':'DIRECT_LEARNING','entry_id':entry,'batch_ordinal':batch,
                'memory_ordinal':ordinal,'binding_requirement':'ACTUAL_MEMORY_REVISION_AND_SOURCE_RECEIPT'})
    for entry,query in zip(('display-real','story-fiction'),QUERIES,strict=True):add('EMBEDDING_QUERY',{'entry_id':entry,'text':query})
    if len(result)!=32 or any(sum(s['role']==role for s in result)!=count for role,count in SLOTS.items()):raise InvalidValue()
    return result


def write_package(directory:Path,repository:Path,configuration:DailyConfigurationCandidate,original_directory:Path) -> dict:
    """Create immutable preparation files; no credentials, databases or grants open."""
    if daily_snapshot_issue(configuration) is not None:raise InvalidValue()
    originals=approved_originals(original_directory)
    paths=sorted(p for p in (repository/'companion_memory').rglob('*') if p.is_file() and p.suffix in ('.py','.json','.txt','.yaml','.toml'))
    paths+= [repository/'pyproject.toml',repository/'uv.lock']
    files=sorted(({'path':str(p.relative_to(repository)),'sha256':sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size} for p in paths),key=lambda v:v['path'])
    code_digest=sha256(''.join(f['path']+'\0'+f['sha256']+'\n' for f in files).encode()).hexdigest()
    config=canonical(candidate_values(configuration))
    if len(config)>2097152:raise InvalidValue()
    directory.mkdir(mode=0o700,parents=False,exist_ok=False)
    material=write_materials(directory/'materials')
    bindings={'code_digest':code_digest,'configuration_digest':sha256(config).hexdigest(),'materials_digest':material['sha256'],
        'fixed_evidence_digests':dict(ORIGINAL_DIGESTS)}
    package={'format':'DAILY_PRE_SEND_PACKAGE_V1','state':'PREPARED_NOT_AUTHORIZED','bindings':bindings,
        'execution_environment':'DOCKER_LINUX_ARM64','files':files,'fixed_material':originals,'slots':purpose_slots(),
        'attempts_used':0,'credential_resolutions':0,'authorization':None,'activation':None,'database_binding':None,
        'fees':{'DeepSeek':{'attempt_limit':18,'currency':'CNY','cost_limit_atoms':30000000,'atom_scale':1000000,
            'rate_verification':'REQUIRED_BEFORE_ACTIVATION','conservative_liability':'REQUIRED_BEFORE_REGISTRATION'},
            'Coding':{'attempt_limit':14,'mode':'USER_ALLOCATED_USAGE','usage':None,'cost':None,'actual_charge':None}},
        'shared_network':{'workers':1,'queue':0,'minimum_interval_after_commit_and_cleanup_seconds':30},
        'automatic_retries':0,'backup_attempts':0,'probe_attempts':0,'reminder_sink':'DISABLED',
        'stop_conditions':['REMOTE_UNKNOWN','AUTHENTICATION_ERROR','IDENTITY_ERROR','PROTOCOL_ERROR','COMMIT_UNCONFIRMED','RESOURCE_LIMIT','CLEANUP_UNFINISHED'],
        'dynamic_binding_rule':'Bind only actual original work, memory revision, source, material, wire and native Provider request before its first registration.',
        'learning_instruction':'每批最多2个MEMORY创建或正文修订目标；其他动作仍共用总8项预算。',
        'recovery_new_sends':0,'real_results':[]}
    from companion_memory.configuration import PresentValue
    accounts=next(e.state.value for e in configuration.foundation.list_entries()
        if e.definition.key=='provider.accounts' and type(e.state) is PresentValue)
    if type(accounts) is not tuple:raise InvalidValue()
    if all(account['billing_mode']=='USAGE_ONLY_TRIAL' for account in cast(tuple[MappingProxyType,...],accounts)):
        package['fees']={name:{'attempt_limit':limit,'mode':'USAGE_ONLY_TRIAL','price':None,
            'cost_limit_atoms':None,'quota':None,'usage':None,'cost':None,'actual_charge':None}
            for name,limit in (('DeepSeek',18),('Coding',14))}
    for name,raw in (('configuration.json',config),('package.json',canonical(package))):
        with (directory/name).open('xb') as stream:stream.write(raw)
    return {'path':str(directory/'package.json'),'sha256':sha256(canonical(package)).hexdigest(),
        'state':package['state'],'bindings':bindings,'slots':32,'attempts_used':0}
