"""Versioned DeepSeek usage persistence, preserving both cache partition reports.

Money uses the existing disjoint token estimator. The original optional cached
alias and mandatory hit/miss observations remain distinct, including nulls.
"""
from types import MappingProxyType
from .values import Record, as_record, freeze, InvalidData
from .token_costs import quantity, add, InvalidAmount

RAW_EXTRA = ('prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')


def normalized(value: Record, observed: Record, *, not_sent: bool) -> Record:
    """Attach actual supplier counts to the already computed native liability."""
    raw = dict(observed)
    for name in RAW_EXTRA:
        raw.setdefault(name, None)
    if not_sent:
        raw = dict.fromkeys(raw, None)
    return as_record(freeze({**value, 'format_version': 4, 'raw_usage': raw}, 4096, owned=True))


def validate(value: Record, *, not_sent: bool) -> Record:
    """Check the closed new version before applying the unchanged money checks."""
    from .text_stored_schema import RAW_FIELDS, validate_usage
    raw = as_record(value['raw_usage'])
    if (type(value['format_version']) is not int or value['format_version'] != 4
            or set(raw) != set(RAW_FIELDS.split()) | set(RAW_EXTRA)
            or value['billing_mode'] != 'TOKEN_METERED'):
        raise InvalidData()
    try:
        for name in RAW_EXTRA:
            if raw[name] is not None:
                quantity(raw[name])
        hit, miss, total = (raw[name] for name in (*RAW_EXTRA, 'prompt_tokens'))
        if value['valid']:
            if hit is not None and miss is not None and total is not None:
                if add(quantity(hit), quantity(miss)) != total:
                    raise InvalidData()
            if raw['cached_tokens'] is not None and raw['cached_tokens'] != hit:
                raise InvalidData()
            if raw['reasoning_tokens'] not in (None, 0):
                raise InvalidData()
        if value['cost_complete'] and not not_sent and any(raw[name] is None for name in (*RAW_EXTRA, 'total_tokens')):
            raise InvalidData()
        if not_sent and any(raw[name] is not None for name in RAW_EXTRA):
            raise InvalidData()
    except InvalidAmount:
        raise InvalidData() from None
    common_raw = {k: v for k, v in raw.items() if k not in RAW_EXTRA}
    common_raw['cached_tokens'] = raw['prompt_cache_hit_tokens']
    validate_usage(MappingProxyType({**value, 'format_version': 2, 'raw_usage': MappingProxyType(common_raw)}), not_sent=not_sent)
    return value
