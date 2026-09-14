"""Explicit MiniMax trial values resolved by the native uniform configuration.

The frozen corpus supplies human-approved semantic material only. No credential
or mutable preparation file is read, and the legacy Ark fixture stays separate.
"""
from pathlib import Path
from companion_memory.configuration.text_resolution import resolve_text_learning_configuration,TextConfigurationOk
from companion_memory.cognition.text_resources import prompt_resource,resource_digest
from tests.text_learning.configuration_support import inputs
from .materials import GENERATION_GOAL,SUPERVISION


def configuration(root: Path):
    """Resolve all 118 explicit entries, retaining actual paths across restarts."""
    supplied=inputs(root)
    foundation=supplied[0]['explicit_values'];text=supplied[5]['explicit_values']
    account=foundation['provider.accounts'][0]
    account.update(account_id='minimax-trial-account',window_id='minimax-authorized-window',
        cost_limit_atoms=0,billing_mode='USAGE_ONLY_TRIAL',quota=None,evidence_ref='operator-token-plan-permission')
    account['price'].update(revision_ref='operator-usage-only-v1',source_url='https://platform.minimax.cn/docs/guides/pricing-token-plan',
        checked_date='2026-09-13',input_atoms_per_million=None,cached_atoms_per_million=None,
        output_atoms_per_million=None,per_attempt_money_bound=None)
    profile=foundation['provider.profiles'][0]
    profile.update(profile_id='minimax-generation',account_id=account['account_id'],model_id='MiniMax-M3',
        wire_protocol='MINIMAX_CHAT_JSON_V1',billing_mode='USAGE_ONLY_TRIAL',max_input_units=1046528)
    foundation['provider.role_profiles']={'LEARNING':['minimax-generation'],'PERSONA':['minimax-generation']}
    text['provider.transport'].update(origin='https://api.minimax.cn',base_path='/v1',secret_ref='trial-llm-secret',
        secret_revision='trial-llm-revision',account_ref=account['account_id'])
    generation=text['provider.generation']
    generation.update(protocol='MINIMAX_CHAT_JSON_V1',model_id='MiniMax-M3',expected_reported_models=['MiniMax-M3'],
        capability_evidence_ref='minimax-chat-official-20260913',billing_evidence_ref='operator-usage-only-v1',
        eligibility_evidence_ref='operator-token-plan-permission',model_context_tokens=1048576,reservation_input_bound=1046528,
        response_mode='JSON_PROMPT_V1',prompt_ref='minimax-learning-json-v1',
        prompt_digest=resource_digest(prompt_resource('LEARNING','MiniMax-M3')))
    text['self_model.initial_persona'].update(generation_goal=GENERATION_GOAL,supervision_prompt=SUPERVISION,
        prompt_ref='minimax-persona-json-v1',prompt_digest=resource_digest(prompt_resource('PERSONA','MiniMax-M3')))
    resolved=resolve_text_learning_configuration(*supplied)
    if type(resolved) is not TextConfigurationOk:raise ValueError('Complete MiniMax configuration rejected: '+repr(resolved))
    return resolved.value,supplied
