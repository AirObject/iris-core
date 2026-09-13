"""Exact token liability arithmetic without a network or ledger side effect.

All intermediate products and sums fit unsigned nonnegative signed-63-bit
storage. Estimates round each mutually exclusive item separately. A reservation
includes the extra atom needed when two positive input items can both round up.
These pure calculations never release an outstanding durable reservation.
"""
from dataclasses import dataclass

MAX_AMOUNT = (1 << 63) - 1
TOKENS_PER_PRICE_UNIT = 1_000_000


class InvalidAmount(ValueError):
    """A quantity, price or intermediate amount cannot be represented safely."""


def quantity(value: object) -> int:
    """Accept an exact nonnegative integer, excluding booleans and subclasses."""
    if type(value) is not int or not 0 <= value <= MAX_AMOUNT:
        raise InvalidAmount('Invalid bounded quantity.')
    return value


def add(*values: int) -> int:
    """Check before every addition, including window totals and quota counts."""
    total = 0
    for value in values:
        value = quantity(value)
        if total > MAX_AMOUNT - value:
            raise InvalidAmount('Amount addition exceeds storage capacity.')
        total += value
    return total


def multiply(left: int, right: int) -> int:
    """Reject an overflowing product even if its eventual quotient would fit."""
    left, right = quantity(left), quantity(right)
    if right and left > MAX_AMOUNT // right:
        raise InvalidAmount('Amount multiplication exceeds storage capacity.')
    return left * right


def rounded_cost(tokens: int, atoms_per_million: int) -> int:
    """Round up without evaluating the potentially overflowing numerator+M-1."""
    quotient, remainder = divmod(multiply(tokens, atoms_per_million), TOKENS_PER_PRICE_UNIT)
    return add(quotient, int(remainder != 0))


@dataclass(frozen=True, slots=True)
class TokenPrices:
    """Three nonoverlapping prices; no currency or vendor price is inferred."""
    uncached: int
    cached: int
    output: int

    def __post_init__(self) -> None:
        for value in (self.uncached, self.cached, self.output):
            quantity(value)


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """Independent item estimates, distinct from a supplier-reported bill."""
    uncached_atoms: int
    cached_atoms: int
    output_atoms: int

    @property
    def total_atoms(self) -> int:
        return add(self.uncached_atoms, self.cached_atoms, self.output_atoms)


def reserve_tokens(input_bound: int, output_bound: int, prices: TokenPrices) -> int:
    """Compute conservative liability before admission; does not spend budget."""
    if type(prices) is not TokenPrices:
        raise InvalidAmount('Native token prices are required.')
    input_bound, output_bound = quantity(input_bound), quantity(output_bound)
    margin = int(input_bound >= 2 and prices.uncached > 0 and prices.cached > 0)
    return add(rounded_cost(input_bound, max(prices.uncached, prices.cached)), margin,
               rounded_cost(output_bound, prices.output))


def estimate_tokens(input_tokens: int, cache_tokens: int, output_tokens: int,
                    input_bound: int, output_bound: int, prices: TokenPrices) -> TokenEstimate:
    """Estimate complete usage; bound violations must retain the old liability."""
    reservation = reserve_tokens(input_bound, output_bound, prices)
    incoming, cached, outgoing = map(quantity, (input_tokens, cache_tokens, output_tokens))
    if cached > incoming or incoming > input_bound or outgoing > output_bound:
        raise InvalidAmount('Usage exceeds its bound liability.')
    estimate = TokenEstimate(rounded_cost(incoming - cached, prices.uncached),
                             rounded_cost(cached, prices.cached), rounded_cost(outgoing, prices.output))
    if estimate.total_atoms > reservation:
        raise InvalidAmount('Usage exceeds its reserved amount.')
    return estimate


def can_reserve(limit: int, known: int, held: int, reserved: int, requested: int) -> bool:
    """Compare disjoint existing responsibility and new liability, without writes."""
    return add(known, held, reserved, requested) <= quantity(limit)
