"""Rational reservation, unknown responsibility and independent quota boundaries.

Prices are arithmetic fixtures, not supplier rates. Complete durable UsageV2 is
validated, including missing token counts and exact integer overflow rejection.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from companion_memory.provider.values import as_record, freeze
from companion_memory.provider.accounting import row
from companion_memory.provider.text_accounting import liability, normalize, check_budget, reserve_budget, settle_budget
from companion_memory.provider.text_stored_schema import validate_usage, validate
from companion_memory.provider.chat_protocol import observe_usage
from companion_memory.provider.token_costs import MAX_AMOUNT
from tests.text_learning.configuration_support import inputs


class TextAccountingTests(unittest.TestCase):
    def setUp(self):
        with TemporaryDirectory() as root:
            values = inputs(Path(root))[0]['explicit_values']
            self.account = as_record(freeze(values['provider.accounts'][0], 4096))
            self.profile = as_record(freeze(values['provider.profiles'][0], 2048))

    def budget(self, account=None):
        account = account or self.account
        return row('budget', format_version=2, account_id=account['account_id'], window_id=account['window_id'], policy=account,
            attempt_count=0, known_subtotal_atoms=0, held_atoms=0, risk_state='CLEAR', quota_reserved=0, quota_known=0, quota_held=0)

    def test_exact_money_boundary_and_atomic_responsibility_replacement(self):
        amount, quota = liability(self.account, self.profile)
        self.assertEqual(quota, 0)
        exact = as_record(freeze({**self.account, 'cost_limit_atoms': amount}, 4096, owned=True))
        low = as_record(freeze({**self.account, 'cost_limit_atoms': amount-1}, 4096, owned=True))
        self.assertIsNone(check_budget(self.budget(exact), amount))
        self.assertEqual(check_budget(self.budget(low), amount), 'COST_LIMIT')
        original = self.budget(exact); reserved = reserve_budget(original, amount)
        raw = as_record(freeze({'prompt_tokens':128000, 'completion_tokens':0, 'total_tokens':128000,
            'prompt_tokens_details':{'cached_tokens':1}}, 2048))
        usage = normalize(observe_usage(raw), exact, self.profile, amount)
        validate_usage(usage)
        self.assertEqual(usage['known_subtotal_atoms'], 30721)
        self.assertEqual(usage['reported_cost_atoms'], None)
        reservation = row('attempt', format_version=2, attempt_id='attempt', account_id='fixture_account', budget_id='budget',
            reserved_atoms=amount, known_subtotal_atoms=0, held_atoms=amount, known_cost_atoms=None, cost_complete=False,
            quota_reserved=0, quota_known=0, quota_held=0)
        settled = settle_budget(reserved, reservation, usage)
        self.assertEqual(settled['known_subtotal_atoms'], 30721); self.assertEqual(settled['held_atoms'], 0)
        self.assertEqual(original['attempt_count'], 0)
        validate('budget_windows', settled)

    def test_absent_partial_invalid_and_over_bound_usage_preserves_full_money(self):
        amount, _ = liability(self.account, self.profile)
        for raw in (None, {'prompt_tokens':1}, {'prompt_tokens':True},
                    {'prompt_tokens':128001,'completion_tokens':0,'prompt_tokens_details':{'cached_tokens':0}},
                    {'prompt_tokens':8,'completion_tokens':0,'prompt_tokens_details':{'cached_tokens':0,'audio_tokens':1}}):
            with self.subTest(raw=raw):
                observation = observe_usage(freeze(raw, 2048))
                usage = normalize(observation, self.account, self.profile, amount)
                validate_usage(usage)
                self.assertFalse(usage['cost_complete']); self.assertEqual(usage['held_atoms'], amount)
                self.assertIsNone(usage['estimated_cost_atoms']); self.assertIsNone(usage['known_cost_atoms'])

    def test_subscription_never_converts_token_counts_to_free_money_or_quota(self):
        account = as_record(freeze({**self.account, 'billing_mode':'SUBSCRIPTION',
            'price':{**as_record(self.account['price']), 'input_atoms_per_million':None,'cached_atoms_per_million':None,
                'output_atoms_per_million':None,'per_attempt_money_bound':10},
            'quota':{'subscription_ref':'fixture_subscription','unit':'SUBSCRIPTION_REQUEST','window_limit':3,
                'per_attempt_bound':2,'consumed_before_test':1,'evidence_ref':'fixture_quota_evidence'}},4096,owned=True))
        profile = as_record(freeze({**self.profile,'billing_mode':'SUBSCRIPTION'},2048,owned=True))
        amount, quota = liability(account, profile); self.assertEqual((amount,quota),(10,2))
        budget = reserve_budget(self.budget(account), amount)
        self.assertEqual(budget['quota_reserved'], 2)
        self.assertEqual(check_budget(budget, amount),'COST_LIMIT')
        observation = observe_usage(freeze({'prompt_tokens':1,'completion_tokens':1,'prompt_tokens_details':{'cached_tokens':0}},2048))
        usage = normalize(observation,account,profile,amount);validate_usage(usage)
        self.assertIsNone(usage['estimated_cost_atoms']);self.assertIsNone(usage['quota_known'])
        self.assertEqual((usage['held_atoms'],usage['quota_held']),(10,2))

    def test_overflowing_window_addition_is_rejected_without_saturation(self):
        budget = as_record(freeze({**self.budget(), 'known_subtotal_atoms':MAX_AMOUNT,'held_atoms':1},8192,owned=True))
        self.assertEqual(check_budget(budget,1),'UNBOUNDED_COST')
