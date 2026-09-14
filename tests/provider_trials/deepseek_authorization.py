"""Explicit independent DeepSeek trial approval, separate from stopped old work.

A proposal is not approval. Activation requires the human's affirmative count,
money and isolation decisions plus the frozen old stop evidence. This validator
has no native Provider or credential capability and never edits an old journal.
"""
from companion_memory.provider.token_costs import TokenPrices, reserve_tokens


def validate_approval(approval: dict) -> None:
    """Reject inferred consent, transferable old slots or diluted money liability."""
    decision=approval['independent_trial']
    required={'new_attempts','cost_limit_atoms','old_attempts','historical_maximum','old_remaining_slots_transferable',
        'old_remote_state','old_cleanup_ended','old_stop_evidence_digest','old_package_digest',
        'new_account_id','new_instances','new_database_ids','new_roots','count_approved','money_approved',
        'independent_under_old_unknown_approved','user_decision_ref'}
    if type(decision) is not dict or set(decision)!=required:raise ValueError('Complete independent trial decision required.')
    if (any(type(decision[k]) is not int for k in ('new_attempts','old_attempts','historical_maximum','cost_limit_atoms'))
            or decision['new_attempts']!=14 or decision['old_attempts']!=10 or decision['historical_maximum']!=24
            or decision['cost_limit_atoms']!=30000000 or decision['old_remote_state']!='REMOTE_RESULT_UNKNOWN'
            or decision['old_remaining_slots_transferable'] is not False or decision['old_cleanup_ended'] is not True
            or any(decision[k] is not True for k in ('count_approved','money_approved','independent_under_old_unknown_approved'))
            or type(decision['user_decision_ref']) is not str or not decision['user_decision_ref'].strip()):
        raise ValueError('The human must explicitly approve count, money and independent execution under old UNKNOWN.')
    for key in ('old_stop_evidence_digest','old_package_digest'):
        v=decision[key]
        if type(v) is not str or len(v)!=64 or any(c not in '0123456789abcdef' for c in v):raise ValueError('Frozen old evidence digest required.')
    if decision['new_account_id']!='deepseek-trial-account':raise ValueError('Independent account identity required.')
    expected_instances={p:'deepseek-trial-'+p for p in ('macos','linux')}
    expected_databases={p:'deepseek-text-trial-'+p for p in ('macos','linux')}
    if decision['new_instances']!=expected_instances or decision['new_database_ids']!=expected_databases:raise ValueError('Independent native identities required.')
    roots=decision['new_roots']
    if (type(roots) is not dict or set(roots)!={'macos','linux'} or len(set(roots.values()))!=2
            or any(type(v) is not str or not v.startswith('/') or 'deepseek' not in v or 'minimax' in v for v in roots.values())):
        raise ValueError('Distinct new absolute roots required.')
    bound=reserve_tokens(1048576,2048,TokenPrices(2000000,40000,8000000))
    if (approval['billing_policy']!='DEEPSEEK_TOKEN_METERED_TRIAL' or approval['cost_limit_atoms']!=30000000
            or approval['per_attempt_money_bound']!=bound or approval['quota_limit']!=0
            or approval['per_attempt_quota_bound']!=0 or approval['extra_operations']!={}):
        raise ValueError('Fixed metered ceiling and no spare allowance required.')
