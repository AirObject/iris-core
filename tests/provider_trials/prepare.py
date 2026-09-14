"""Create a complete nonsecret review package without constructing a live sender.

All 118 configuration entries retain their native definition metadata. Unproven
supplier quantities are null in this review-only proposal, so it cannot become
an executable configuration. Existing user preparation files are not read here.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import cast
from companion_memory.persistence import Value
from tempfile import TemporaryDirectory
from companion_memory.configuration.text_codec import candidate_values
from companion_memory.cognition.text_resources import output_schema,LEARNING_INSTRUCTIONS,PERSONA_INSTRUCTIONS
from tests.text_learning.configuration_support import candidate
from .materials import corpus,GENERATION_GOAL,SUPERVISION
from .review import annotation_template
from .files import canonical,digest,write_new


MATERIAL_APPROVAL='Human approval of complete outbound materials and gold targets is pending.'
BLOCKERS=(
 'MiniMax-M3 strict JSON Schema support and full input liability remain unverified; no capability probe is permitted.',
 'Native executable MiniMax binding and usage-only settlement require the supplier protocol decision; review values are not runtime defaults.',
 MATERIAL_APPROVAL,
 'Controlled HTTPS Linux egress and live per-operation monitoring require a qualified final launch profile.',
)



def configuration_proposal(root: str) -> dict:
    """Export all native entries; path placeholders are review data, never defaults."""
    with TemporaryDirectory() as directory:
        source=Path(directory);value,_=candidate(source)
        encoded=candidate_values(value)
        domains=[]
        for domain in encoded['domains']:
            entries=[]
            for entry in domain['entries']:
                decoded=json.loads(entry['body'].replace(str(source),root))
                entries.append({'parameter_key':entry['parameter_key'],**decoded})
            domains.append({'domain_id':domain['domain_id'],'entries':entries})
    by_key={entry['parameter_key']:entry for domain in domains for entry in domain['entries']}
    # Values use the configuration codec's typed containers, not plain JSON.
    # Produce a separate readable value vector; retain the full encoded native
    # proposal as evidence so no definition or source field is silently dropped.
    from companion_memory.configuration.content_codec import decode_content_entry,dump
    from companion_memory.configuration.persistent_codec import _encode
    from companion_memory.ingress.events import plain
    values={key:plain(cast(Value,decode_content_entry(dump({k:v for k,v in entry.items() if k!='parameter_key'}))[2])) for key,entry in by_key.items()}
    account=cast(list[dict],values['provider.accounts'])[0]
    account.update(account_id='trial-account',window_id='pending-window',currency='CNY',billing_mode='SUBSCRIPTION',cost_limit_atoms=None,evidence_ref='pending-account-evidence')
    account['price'].update(source_url='https://platform.minimax.cn/docs/guides/pricing-token-plan',revision_ref='pending-price',checked_date=None,input_atoms_per_million=None,cached_atoms_per_million=None,output_atoms_per_million=None,per_attempt_money_bound=None)
    account['quota']={'subscription_ref':'pending-subscription','unit':'SUBSCRIPTION_REQUEST','window_limit':None,
        'per_attempt_bound':None,'consumed_before_test':None,'evidence_ref':'pending-quota-evidence'}
    profile=cast(list[dict],values['provider.profiles'])[0]
    profile.update(profile_id='trial-generation',account_id='trial-account',model_id='MiniMax-M3',max_input_units=None,billing_mode='SUBSCRIPTION')
    values['provider.role_profiles']={'LEARNING':['trial-generation'],'PERSONA':['trial-generation']}
    cast(dict,values['provider.transport']).update(origin='https://api.minimax.cn',base_path='/v1',secret_ref='trial-llm-secret',secret_revision='trial-llm-revision',account_ref='trial-account')
    cast(dict,values['provider.generation']).update(model_id='MiniMax-M3',expected_reported_models=['MiniMax-M3'],reservation_input_bound=None,model_context_tokens=None,
        capability_evidence_ref='pending-capability',billing_evidence_ref='pending-billing',eligibility_evidence_ref='pending-eligibility')
    cast(dict,values['self_model.initial_persona']).update(generation_goal=GENERATION_GOAL,supervision_prompt=SUPERVISION)
    for key,entry in by_key.items():entry['value']=_encode(values[key])
    return {'status':'REVIEW_ONLY_NOT_EXECUTABLE','entry_count':len(by_key),'domains':domains,'values':values,
        'operator_trial_policy':{'subscription':'MiniMax Token Plan','usage_only':True,'reported_trial_cost_yuan':0,'reported_cost_source':'OPERATOR_CONVENTION_NOT_SUPPLIER_BILL','minimum_seconds_after_cleanup':30},
        'shared_budget':{'standard_attempts':14,'maximum_attempts':16,'extra_attempts_require_separate_purpose_approval':2,
            'approved_cost_limit_atoms':None,'approved_quota_limit':None,'reservation_formula':'remaining money and quota must cover all unresolved + next reservation',
            'resets':'none; the authorization is shared across platforms, restarts and explicit retries'}}


def prepare(output: Path) -> str:
    """Write a fresh review directory; preserve every prior package version."""
    output.mkdir(mode=0o700)
    packet={'materials':corpus(),'configuration':{platform:configuration_proposal(f'<{platform.upper()}_PRIVATE_ROOT>') for platform in ('macos','linux')},
            'blockers':list(BLOCKERS),'authorization':None,'human_quality_thresholds':{'precision':0.90,'recall':0.80,'anchor_support':1.0,'permission_violations':0}}
    checksum=digest(packet)
    write_new(output/'package.json',canonical(packet))
    write_new(output/'package.sha256',(checksum+'\n').encode())
    for role,instructions in (('LEARNING',LEARNING_INSTRUCTIONS),('PERSONA',PERSONA_INSTRUCTIONS)):
        write_new(output/(role.lower()+'-instructions.txt'),instructions.encode())
        write_new(output/(role.lower()+'-schema.json'),output_schema(role))
    for platform in ('macos','linux'):
        write_new(output/(platform+'-human-annotations.json'),canonical(annotation_template(platform)))
    lines=['# 真实文本试验待审包','',f'包摘要：`{checksum}`。状态：未批准发送，配置尚不可执行。','',
           '两平台同一份合成语义材料，各自实际数据库与身份。所有标准答案只供人工预审，不加入模型请求。','',
           '## 初始自我与监管目标','',corpus()['initial_self'],'',GENERATION_GOAL,'',SUPERVISION,'',
           '## 完整材料及实际窗口','']
    data=corpus()
    for message in cast(list[dict],data['messages']):lines.extend([f"### 消息 {message['ordinal']}",'',message['body'],''])
    for batch in cast(list[dict],data['batches']):lines.append(f"- {batch['name']}：H={batch['history']}；T={batch['target']}；R={batch['recent']}")
    lines.extend(['','## 应学习原子命题候选',''])
    for target in cast(list[dict],data['expected_propositions']):
        lines.append(f"- {target['id']}：{target['text']} — 目标消息 {target['message_ordinal']}，BODY 整段锚点；{target['category']}/{target['stance']}；{target['subject']}；{target['world']}")
    lines.extend(['','消息5用于阻止无依据推断，消息8/9预期零记忆；消息11仅攻击，消息12永远只是辅助。其他可能值得记住的命题须在发送前由用户修订标准集。',
        '', '## 外发封套', '', '精确SYSTEM模板与JSON Schema见相邻文件。USER包含完整H/T/R事件、已审核persona、受控身份及数据库/批次/来源/配置的非秘密ID；相关记忆固定空集合。标准答案和审核标注不发送。',
        '', '完整118键及全部定义见package.json。数值null和pending引用不可用于真实启动，候选路径不是运行目录。', '', '## 发送前集中缺口',''])
    lines.extend('- '+item for item in BLOCKERS)
    write_new(output/'REVIEW.md',('\n'.join(lines)+'\n').encode())
    return checksum


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    args=parser.parse_args();print(prepare(args.output))

if __name__=='__main__':main()
