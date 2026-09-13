"""Exact generation liability and conservative token/subscription settlement.

Three token cost items are mutually exclusive and round independently. Unknown
usage, unsupported billing or arithmetic overflow retains the original money
and quota responsibility. No supplier bill or subscription count is invented.
"""
from types import MappingProxyType
from typing import cast
from .accounting import revise
from .chat_protocol import UsageObservation, observe_usage
from .token_costs import TokenPrices, InvalidAmount, add, quantity, reserve_tokens, estimate_tokens
from .values import Data, Record, as_record, freeze, InvalidData


def prices(account: Record) -> TokenPrices:
    """Read exact rational prices already validated by the configuration owner."""
    price = as_record(account['price'])
    return TokenPrices(*(quantity(price[key]) for key in
        ('input_atoms_per_million', 'cached_atoms_per_million', 'output_atoms_per_million')))


def liability(account: Record, profile: Record) -> tuple[int, int]:
    """Money and independent subscription quota reserved for a single attempt."""
    if account['billing_mode'] == 'TOKEN_METERED':
        return reserve_tokens(quantity(profile['max_input_units']), quantity(profile['max_output_units']), prices(account)), 0
    return quantity(as_record(account['price'])['per_attempt_money_bound']), quantity(as_record(account['quota'])['per_attempt_bound'])


def normalize(observation: UsageObservation, account: Record, profile: Record, reserved: int,
              *, not_sent: bool = False) -> Record:
    """Build bounded UsageV2 independently of output validity and HTTP success."""
    if type(observation) is not UsageObservation:
        raise InvalidData()
    money, quota = liability(account, profile)
    if reserved not in (0, money) or reserved != money and not not_sent:
        raise InvalidData()
    fields = observation.fields
    incoming, cached, outgoing = (fields[name] for name in ('input_tokens', 'cache_read_tokens', 'output_tokens'))
    items: list[Data] = []
    estimated: int | None = None
    valid = observation.valid
    covered = observation.billing_covered
    if not_sent:
        observation = observe_usage(None)
        fields = observation.fields
        incoming = cached = outgoing = 0
        covered = valid = True
    if account['billing_mode'] == 'TOKEN_METERED':
        rates = prices(account)
        amounts: tuple[int | None, ...] = (None, None, None)
        quantities: tuple[Data, ...] = (None, cached, outgoing)
        if incoming is not None and cached is not None:
            quantities = (cast(int, incoming) - cast(int, cached), cached, outgoing)
        if covered:
            try:
                estimate = estimate_tokens(quantity(incoming), quantity(cached), quantity(outgoing),
                    quantity(profile['max_input_units']), quantity(profile['max_output_units']), rates)
                amounts = (estimate.uncached_atoms, estimate.cached_atoms, estimate.output_atoms)
                estimated = estimate.total_atoms
            except InvalidAmount:
                covered = False
        for name, count, rate, amount in zip(('input', 'cached_input', 'output'), quantities,
                (rates.uncached, rates.cached, rates.output), amounts, strict=True):
            items.append(MappingProxyType({'item': name, 'quantity': count if type(count) is int and count >= 0 else None,
                'price_numerator': rate, 'price_denominator': 1000000, 'cost_atoms': amount}))
    else:
        # Token observations never prove a subscription debit or a money bill.
        estimated = 0 if not_sent else None
        covered = not_sent
        items.append(MappingProxyType({'item': 'subscription_request', 'quantity': 0 if not_sent else None,
            'price_numerator': None, 'price_denominator': None, 'cost_atoms': estimated}))
    complete = bool(covered and estimated is not None)
    subtotal = estimated if complete else 0
    return as_record(freeze({'format_version': 2, 'fields': fields, 'raw_usage': observation.raw_usage,
        'source': 'LOCALLY_ESTIMATED' if estimated is not None else 'UNAVAILABLE',
        'coverage': 'COMPLETE' if covered else 'PARTIAL' if any(v is not None for v in fields.values()) else 'UNAVAILABLE',
        'cost_complete': complete, 'known_cost_atoms': estimated if complete else None,
        'known_subtotal_atoms': subtotal, 'held_atoms': 0 if complete else reserved,
        'estimated_cost_atoms': estimated, 'reported_cost_atoms': None,
        'price_revision': as_record(account['price'])['revision_ref'], 'items': tuple(items),
        'valid': valid, 'cost_disagreement': False, 'currency': account['currency'], 'atom_scale': account['atom_scale'],
        'billing_mode': account['billing_mode'], 'quota_known': 0 if not_sent or quota == 0 else None,
        'quota_held': 0 if not_sent else quota}, 4096, owned=True))


def check_budget(budget: Record, amount: int) -> str | None:
    """Reject overflowing or insufficient money/quota before an attempt is owned."""
    account = as_record(budget['policy'])
    if budget['risk_state'] != 'CLEAR':
        return 'RESERVATION_OVERRUN'
    if quantity(budget['attempt_count']) >= quantity(account['attempt_limit']):
        return 'ATTEMPT_LIMIT'
    try:
        if add(quantity(budget['known_subtotal_atoms']), quantity(budget['held_atoms']), amount) > quantity(account['cost_limit_atoms']):
            return 'COST_LIMIT'
        if account['quota'] is not None:
            quota = as_record(account['quota'])
            total = add(quantity(quota['consumed_before_test']), quantity(budget['quota_reserved']),
                quantity(budget['quota_known']), quantity(budget['quota_held']), quantity(quota['per_attempt_bound']))
            if total > quantity(quota['window_limit']):
                return 'COST_LIMIT'
    except InvalidAmount:
        return 'UNBOUNDED_COST'
    return None


def reserve_budget(budget: Record, amount: int) -> Record:
    """Apply checked responsibility in the same transaction as the preparation."""
    if check_budget(budget, amount) is not None:
        raise InvalidData()
    account = as_record(budget['policy'])
    quota = quantity(as_record(account['quota'])['per_attempt_bound']) if account['quota'] is not None else 0
    return revise(budget, attempt_count=add(quantity(budget['attempt_count']), 1),
        held_atoms=add(quantity(budget['held_atoms']), amount), quota_reserved=add(quantity(budget['quota_reserved']), quota))


def settle_budget(budget: Record, reservation: Record, usage: Record) -> Record:
    """Replace only this attempt's disjoint liabilities; replay never double spends."""
    def replace_total(name: str, old: int, new: int) -> int:
        total = quantity(budget[name])
        if old > total:
            raise InvalidData()
        return add(total - old, new)
    return revise(budget,
        known_subtotal_atoms=replace_total('known_subtotal_atoms', quantity(reservation['known_subtotal_atoms']), quantity(usage['known_subtotal_atoms'])),
        held_atoms=replace_total('held_atoms', quantity(reservation['held_atoms']), quantity(usage['held_atoms'])),
        quota_reserved=replace_total('quota_reserved', quantity(reservation['quota_reserved']), 0),
        quota_known=replace_total('quota_known', quantity(reservation['quota_known']), quantity(usage['quota_known'] or 0)),
        quota_held=replace_total('quota_held', quantity(reservation['quota_held']), quantity(usage['quota_held'])),
        risk_state='RESERVATION_OVERRUN' if not usage['cost_complete'] and any(v is not None for v in as_record(usage['fields']).values()) else budget['risk_state'])
