"""Cumulative attempt admission for priced, trial and ordinary usage accounts.

A null limit belongs only to the ordinary unpriced policy. It never cancels
unknown remote outcomes, occupied transports or bounded individual requests.
"""
from .values import Record, InvalidData


def attempt_limit_reached(account: Record, attempts: int) -> bool:
    """Check the frozen account without treating missing trial limits as unlimited."""
    limit = account['attempt_limit']
    if limit is None:
        if account['billing_mode'] != 'USAGE_ONLY':
            raise InvalidData()
        return False
    if type(limit) is not int or type(attempts) is not int or attempts < 0:
        raise InvalidData()
    return attempts >= limit
