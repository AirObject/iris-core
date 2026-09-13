"""Check item rounding and conservative liability independently of any supplier.

The integer oracle uses exact fractions. These tests do not claim ledger
settlement, subscriptions, actual prices or remote usage qualification.
"""
from fractions import Fraction
from itertools import product
import math
import unittest
from companion_memory.provider.token_costs import (
    MAX_AMOUNT, InvalidAmount, TokenPrices, add, multiply, quantity,
    reserve_tokens, estimate_tokens, rounded_cost, can_reserve,
)


class TokenCostTests(unittest.TestCase):
    def test_cache_split_requires_extra_atom(self):
        prices = TokenPrices(240000, 120000, 0)
        estimate = estimate_tokens(128000, 1, 0, 128000, 0, prices)
        self.assertEqual((estimate.uncached_atoms, estimate.cached_atoms), (30720, 1))
        self.assertEqual(estimate.total_atoms, 30721)
        self.assertEqual(reserve_tokens(128000, 0, prices), 30721)
        self.assertTrue(can_reserve(30721, 0, 0, 0, 30721))
        self.assertFalse(can_reserve(30720, 0, 0, 0, 30721))

    def test_boundary_grid_matches_fraction_oracle(self):
        rates = (0, 1, 120000, 240000, 1000000)
        for bound, uncached, cached, output in product((0, 1, 2, 7, 128000), rates, rates, rates):
            for cached_price, output_price in ((cached, output), (output, cached)):
                prices = TokenPrices(uncached, cached_price, output_price)
                reserved = reserve_tokens(bound, 2048, prices)
                for incoming in sorted({0, min(1, bound), min(2, bound), bound}):
                    for cache in sorted({0, min(1, incoming), max(0, incoming - 1), incoming}):
                        for outgoing in (0, 1, 2048):
                            estimate = estimate_tokens(incoming, cache, outgoing, bound, 2048, prices)
                            oracle = sum(math.ceil(Fraction(n * p, 1000000)) for n, p in (
                                (incoming-cache, uncached), (cache, cached_price), (outgoing, output_price)))
                            self.assertEqual(estimate.total_atoms, oracle)
                            self.assertLessEqual(oracle, reserved)

    def test_small_exhaustive_input_splits(self):
        rates = (0, 1, 999999, 1000000, 1000001)
        for bound, a, b in product(range(9), rates, rates):
            prices = TokenPrices(a, b, 1000001)
            reserve = reserve_tokens(bound, 3, prices)
            for incoming in range(bound+1):
                for cache in range(incoming+1):
                    self.assertLessEqual(estimate_tokens(incoming, cache, 3, bound, 3, prices).total_atoms, reserve)

    def test_all_quantities_are_exact_bounded_integers(self):
        class Integer(int):
            pass
        for bad in (True, False, -1, 1.0, '1', None, Integer(1), MAX_AMOUNT+1):
            with self.subTest(value=repr(bad)), self.assertRaises(InvalidAmount):
                quantity(bad)

    def test_intermediate_overflow_is_rejected(self):
        self.assertEqual(multiply(MAX_AMOUNT, 1), MAX_AMOUNT)
        self.assertEqual(add(MAX_AMOUNT, 0), MAX_AMOUNT)
        self.assertEqual(rounded_cost(MAX_AMOUNT, 1), math.ceil(Fraction(MAX_AMOUNT, 1000000)))
        for operation in (lambda: add(MAX_AMOUNT, 1), lambda: multiply(MAX_AMOUNT, 2),
                          lambda: rounded_cost(MAX_AMOUNT, 2),
                          lambda: can_reserve(MAX_AMOUNT, MAX_AMOUNT, 1, 0, 0)):
            with self.assertRaises(InvalidAmount):
                operation()
        # Every product is individually legal; their rounded sum overflows.
        with self.assertRaises(InvalidAmount):
            add(MAX_AMOUNT, 1, rounded_cost(1, 1))

    def test_usage_cannot_exceed_frozen_liability(self):
        for incoming, cached, outgoing in ((11, 0, 1), (1, 2, 1), (1, 0, 3)):
            with self.assertRaises(InvalidAmount):
                estimate_tokens(incoming, cached, outgoing, 10, 2, TokenPrices(1, 1, 1))

    def test_money_and_quota_window_do_not_saturate(self):
        self.assertTrue(can_reserve(100, 20, 30, 10, 40))
        self.assertFalse(can_reserve(99, 20, 30, 10, 40))
        with self.assertRaises(InvalidAmount):
            can_reserve(MAX_AMOUNT, MAX_AMOUNT-1, 1, 0, 1)
