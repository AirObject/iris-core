"""Complete proposed DeepSeek values, resolved through uniform configuration.

No secret or user preparation file is read. These values establish technical
limits only; a separate explicit cross-platform authorization is required to
activate a sender. Peak prices are conservative estimates, never supplier bills.
"""
from pathlib import Path
from companion_memory.configuration.text_resolution import resolve_text_learning_configuration, TextConfigurationOk
from companion_memory.cognition.text_resources import prompt_resource, resource_digest
from tests.text_learning.configuration_support import inputs
from .materials import GENERATION_GOAL, SUPERVISION

PRICE_SOURCE = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
PRICE_CHECKED = '2026-09-13'
INPUT_BOUND = 1048576


def configuration(root: Path):
    """Resolve all 118 entries for a new isolated trial; do not activate a grant."""
    supplied = inputs(root)
    foundation = supplied[0]['explicit_values']
    supplied[1]['explicit_values']['runtime.operation_timeout_ms'] = 60000
    text = supplied[5]['explicit_values']
    account = foundation['provider.accounts'][0]
    account.update(account_id='deepseek-trial-account', window_id='deepseek-proposed-window',
        attempt_limit=14, cost_limit_atoms=30000000, billing_mode='TOKEN_METERED', quota=None,
        evidence_ref='deepseek-proposed-authorization')
    account['price'].update(revision_ref='deepseek-flash-peak-20260913', source_url=PRICE_SOURCE,
        checked_date=PRICE_CHECKED, input_atoms_per_million=2000000,
        cached_atoms_per_million=40000, output_atoms_per_million=8000000,
        per_attempt_money_bound=None)
    profile = foundation['provider.profiles'][0]
    profile.update(profile_id='deepseek-generation', account_id=account['account_id'],
        model_id='deepseek-flash', wire_protocol='DEEPSEEK_CHAT_JSON_V1',
        billing_mode='TOKEN_METERED', max_input_units=INPUT_BOUND)
    foundation['provider.role_profiles'] = {'LEARNING': ['deepseek-generation'], 'PERSONA': ['deepseek-generation']}
    text['provider.transport'].update(origin='https://api.deepseek.com', base_path='',
        secret_ref='deepseek-trial-secret', secret_revision='deepseek-trial-secret-revision',
        account_ref=account['account_id'])
    text['provider.generation'].update(protocol='DEEPSEEK_CHAT_JSON_V1', model_id='deepseek-flash',
        expected_reported_models=['deepseek-flash'], resolved_model_id=None,
        capability_evidence_ref='deepseek-chat-official-20260913',
        billing_evidence_ref='deepseek-flash-peak-20260913', eligibility_evidence_ref='deepseek-proposed-authorization',
        model_context_tokens=1048576, reservation_input_bound=INPUT_BOUND,
        response_mode='JSON_OBJECT_V1', prompt_ref='deepseek-learning-json-v2',
        prompt_digest=resource_digest(prompt_resource('LEARNING', 'deepseek-flash')))
    text['self_model.initial_persona'].update(generation_goal=GENERATION_GOAL, supervision_prompt=SUPERVISION,
        prompt_ref='deepseek-persona-json-v2', prompt_digest=resource_digest(prompt_resource('PERSONA', 'deepseek-flash')))
    resolved = resolve_text_learning_configuration(*supplied)
    if type(resolved) is not TextConfigurationOk:
        raise ValueError('Complete DeepSeek configuration rejected: ' + repr(resolved))
    return resolved.value, supplied
