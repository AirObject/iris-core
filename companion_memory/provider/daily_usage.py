"""Daily token observations without a price table or monetary admission.

Input and cache counts form disjoint quantities. Supplier usage coverage, remote
outcome and unknown money are independent. Only proven no-send evidence carries
zero known cost; an unreported bill for a sent request remains null.
"""
from typing import cast
from .chat_protocol import UsageObservation
from .values import Record, as_record, freeze, InvalidData
from .token_costs import quantity, add, InvalidAmount


def normalize(observation: UsageObservation, account: Record, profile: Record, *, not_sent: bool = False) -> Record:
    if (account['billing_mode'] != 'USAGE_ONLY_TRIAL' or account['price'] is not None
            or account['cost_limit_atoms'] is not None or account['quota'] is not None):
        raise InvalidData()
    deepseek = cast(str, profile['wire_protocol']).startswith('DEEPSEEK_')
    if not_sent:
        from .deepseek_protocol import observe_usage as deepseek_usage
        from .minimax_protocol import observe_usage as minimax_usage
        observation = (deepseek_usage if deepseek else minimax_usage)(None)
    fields = observation.fields
    incoming, cached, outgoing = (fields[k] for k in ('input_tokens', 'cache_read_tokens', 'output_tokens'))
    valid = observation.valid
    for count, bound in ((incoming, profile['max_input_units']), (outgoing, profile['max_output_units'])):
        if count is not None and cast(int, count) > cast(int, bound):
            valid = False
    uncached = cast(int, incoming) - cast(int, cached) if incoming is not None and cached is not None and cast(int,incoming) >= cast(int,cached) else None
    quantities = (uncached, cached, outgoing)
    observed = any(v is not None for v in fields.values())
    result = as_record(freeze({
        'format_version': 5, 'fields': fields, 'raw_usage': observation.raw_usage,
        'source': 'PROVIDER_REPORTED' if observed else 'UNAVAILABLE',
        'coverage': 'COMPLETE' if not_sent or valid and observation.billing_covered else 'PARTIAL' if observed else 'UNAVAILABLE',
        'cost_complete': not_sent, 'known_cost_atoms': 0 if not_sent else None,
        'known_subtotal_atoms': 0, 'held_atoms': 0, 'estimated_cost_atoms': None,
        'reported_cost_atoms': None, 'price_revision': None,
        'items': tuple({'item': name, 'quantity': 0 if not_sent else count, 'price_numerator': None,
            'price_denominator': None, 'cost_atoms': 0 if not_sent else None}
            for name, count in zip(('input', 'cached_input', 'output'), quantities, strict=True)),
        'valid': valid, 'cost_disagreement': False, 'currency': account['currency'],
        'atom_scale': account['atom_scale'], 'billing_mode': account['billing_mode'],
        'quota_known': None, 'quota_held': 0}, 4096, owned=True))
    validate(result, deepseek=deepseek, not_sent=not_sent)
    return result


def validate(value: Record, *, deepseek: bool, not_sent: bool) -> None:
    """Check the already schema-isolated record without manufacturing price evidence."""
    raw = as_record(value['raw_usage']); fields = as_record(value['fields'])
    mappings = [('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
        ('total_tokens', 'total_tokens'), ('reasoning_tokens', 'reasoning_tokens'),
        ('cache_read_tokens', 'prompt_cache_hit_tokens' if deepseek else 'cached_tokens')]
    if any(fields[a] != raw[b] for a, b in mappings):
        raise InvalidData()
    if any(fields[k] is not None for k in ('cache_write_tokens', 'input_items', 'embedding_dimensions',
            'rerank_candidates', 'media_bytes', 'media_duration_ms')):
        raise InvalidData()
    incoming, cached, outgoing, total = (fields[k] for k in ('input_tokens', 'cache_read_tokens', 'output_tokens', 'total_tokens'))
    try:
        if value['valid']:
            if all(v is not None for v in (incoming, outgoing, total)) and add(quantity(incoming), quantity(outgoing)) != total:
                raise InvalidData()
            if incoming is not None and cached is not None and cast(int,cached) > cast(int,incoming):
                raise InvalidData()
            if deepseek:
                miss = raw['prompt_cache_miss_tokens']
                if incoming is not None and cached is not None and miss is not None and add(quantity(cached), quantity(miss)) != incoming:
                    raise InvalidData()
                if raw['cached_tokens'] is not None and raw['cached_tokens'] != cached or fields['reasoning_tokens'] not in (None, 0):
                    raise InvalidData()
            elif fields['reasoning_tokens'] is not None and outgoing is not None and cast(int,fields['reasoning_tokens']) > cast(int,outgoing):
                raise InvalidData()
    except InvalidAmount:
        raise InvalidData() from None
    observed = any(v is not None for v in fields.values())
    if value['source'] != ('PROVIDER_REPORTED' if observed else 'UNAVAILABLE'):
        raise InvalidData()
    if value['coverage'] == 'UNAVAILABLE' and observed:
        raise InvalidData()
    if value['coverage'] == 'COMPLETE' and not not_sent and (not value['valid'] or any(v is None for v in (incoming, outgoing, total))):
        raise InvalidData()
    if deepseek and value['coverage'] == 'COMPLETE' and not not_sent and (cached is None or raw['prompt_cache_miss_tokens'] is None):
        raise InvalidData()
    if not_sent and any(v is not None for v in raw.values()):
        raise InvalidData()
    if (value['cost_complete'] is not not_sent or value['known_cost_atoms'] != (0 if not_sent else None)
            or any(value[k] is not None for k in ('price_revision', 'reported_cost_atoms', 'estimated_cost_atoms', 'quota_known'))
            or any(value[k] != 0 for k in ('known_subtotal_atoms', 'held_atoms', 'quota_held')) or value['cost_disagreement']):
        raise InvalidData()
    uncached = cast(int, incoming) - cast(int, cached) if incoming is not None and cached is not None and cast(int,incoming) >= cast(int,cached) else None
    for part, name, count in zip(cast(tuple[Record, ...], value['items']), ('input', 'cached_input', 'output'), (uncached, cached, outgoing), strict=True):
        if (part['item'] != name or part['quantity'] != (0 if not_sent else count)
                or part['price_numerator'] is not None or part['price_denominator'] is not None
                or part['cost_atoms'] != (0 if not_sent else None)):
            raise InvalidData()
