"""Native setup defaults, compact external inputs and exact production bindings.

Only this test's explicit external material is synthetic. Production resource
bytes come from their owners, and no credential is read or model request sent.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import cast
import unittest

from companion_memory.cognition import daily_resources, dream_resources
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.configuration.managed_bootstrap import bootstrap_snapshot
from companion_memory.configuration.managed_diagnostics import diagnose_managed_configuration
from companion_memory.configuration.managed_form import form_view
from companion_memory.configuration.managed_registry import initial_values, registries, resolve_values
from companion_memory.configuration.managed_resolution import ManagedConfigurationErr, ManagedConfigurationOk
from companion_memory.configuration.managed_setup import setup_presentation
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.credentials import CredentialResolver, CredentialUnavailable
from companion_memory.provider.daily_protocol import DailyChatBinding
from companion_memory.provider.dream_protocol import DreamChatBinding, ROLES as DREAM_ROLES
from companion_memory.provider.text_accounting import prices
from companion_memory.provider.values import as_record, freeze


def set_path(values: dict[str, object], address: dict[str, object], value: object) -> None:
    """Write exactly one advertised compact address in an explicit test document."""
    domain, key = address['domain'], address['key']
    assert type(domain) is str and type(key) is str
    target = values[domain]
    assert type(target) is dict
    current = target[key]
    path = address['path']
    assert type(path) is list and path
    for part in path[:-1]:
        if type(part) is int:
            assert type(current) is list
            current = current[part]
        else:
            assert type(part) is str and type(current) is dict
            current = current[part]
    last = path[-1]
    if type(last) is int:
        assert type(current) is list
        current[last] = deepcopy(value)
    else:
        assert type(last) is str and type(current) is dict
        current[last] = deepcopy(value)


def fill_external_fields(values: dict[str, object]) -> None:
    """Supply only advertised genuine inputs and mirrors, never replace defaults."""
    fields = setup_presentation()['fields']
    assert type(fields) is list
    for field in fields:
        assert type(field) is dict
        path = field['path']
        assert type(path) is list
        name = path[-1]
        supplied = {
            'secret_ref': 'synthetic_external_credential',
            'evidence_ref': 'synthetic_operator_declaration',
            'input_atoms_per_million': 2000000,
            'cached_atoms_per_million': 100000,
            'output_atoms_per_million': 3000000,
            'source_url': 'https://example.invalid/synthetic-prices',
            'checked_date': '2026-09-26',
            'server_evidence_ref': 'synthetic_compatibility_evidence',
            'sdk_evidence_digest': sha256(b'explicit synthetic SDK evidence').hexdigest(),
        }
        if name not in supplied:
            continue
        set_path(values, field, supplied[name])
        for mirror in field['mirrors']:
            set_path(values, mirror, supplied[name])


class SetupDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = resolve_deployment({'deployment.data_root': self.temporary.name})
        self.directories = {
            'media': (str(self.root / 'blobs'), str(self.root / 'upload_staging')),
            'database': (str(self.root / 'db'),), 'audit': (str(self.root / 'db'),),
            'provider_usage': (str(self.root / 'db'),),
            'backup': (str(self.root / 'backups'), str(self.root / 'backup_staging')),
        }

    def values(self):
        return initial_values(self.settings, self.directories, 'Asia/Shanghai')

    def resolve(self, values):
        return resolve_values(self.settings, self.directories, 'default_platform', values, require_price_evidence=True)

    def diagnose(self, values):
        return diagnose_managed_configuration(self.settings, self.directories, 'default_platform', values)

    def test_only_compact_external_material_is_needed_by_complete_resolver(self):
        values = self.values()
        before = deepcopy(values)
        issues = self.diagnose(values)
        self.assertFalse(issues['issues_truncated'])
        self.assertGreater(len(issues['issues']), 0)
        self.assertLess(len(issues['issues']), 32)
        self.assertIs(type(self.resolve(values)), ManagedConfigurationErr)
        fill_external_fields(values)
        result = self.resolve(values)
        self.assertIs(type(result), ManagedConfigurationOk, result)
        self.assertEqual(self.diagnose(values), {'issues': [], 'issues_truncated': False})
        self.assertEqual(self.values(), before)
        foundation = values['foundation']
        assert type(foundation) is dict
        self.assertEqual([account['attempt_limit'] for account in foundation['provider.accounts']], [16, 16])
        self.assertEqual(foundation['provider.accounts'][0]['cost_limit_atoms'], 5000000)
        self.assertEqual(foundation['provider.accounts'][0]['billing_mode'], 'TOKEN_METERED')
        self.assertEqual(foundation['provider.accounts'][1]['billing_mode'], 'USAGE_ONLY_TRIAL')
        self.assertIsNone(foundation['provider.accounts'][1]['cost_limit_atoms'])
        prices(as_record(freeze(foundation['provider.accounts'][0], 4096, owned=True)))

    def test_shipped_resource_bytes_construct_actual_protocol_and_transport_bindings(self):
        values = self.values()
        fill_external_fields(values)
        result = self.resolve(values)
        assert type(result) is ManagedConfigurationOk
        text = values['text']
        assert type(text) is dict
        credential_reads: list[str] = []

        def unavailable(reference: str, revision: str, account: str):
            credential_reads.append(reference)
            return CredentialUnavailable('UNAVAILABLE')

        resolver = CredentialResolver(unavailable)
        for raw in text['provider.transport']['roles']:
            setting = as_record(freeze(raw, 8192, owned=True))
            role = raw['role']
            owner = dream_resources if role in DREAM_ROLES else daily_resources
            binding_type = DreamChatBinding if role in DREAM_ROLES else DailyChatBinding
            binding_type(role, 'deepseek-flash', raw['schema_ref'], raw['schema_digest'],
                owner.output_schema(role), raw['prompt_digest'], owner.prompt_resource(role))
            transport = ChatTransport.dream(setting, resolver, time.monotonic) if role in DREAM_ROLES else ChatTransport.daily(setting, resolver, time.monotonic)
            self.assertIs(type(transport), ChatTransport)
        embedding = as_record(freeze(text['provider.embedding_transport'], 8192, owned=True))
        self.assertIs(type(ChatTransport.for_embedding(embedding, resolver, 'embedding_account', time.monotonic)), ChatTransport)
        self.assertEqual(credential_reads, [])

    def test_missing_metered_rates_and_price_evidence_never_validate(self):
        for field, invalid in (('input_atoms_per_million', None), ('cached_atoms_per_million', None),
                ('output_atoms_per_million', None), ('source_url', ''), ('source_url', ' \t'),
                ('checked_date', ''), ('checked_date', '2026-02-30'), ('checked_date', '20260926')):
            with self.subTest(field=field, invalid=invalid):
                values = self.values()
                fill_external_fields(values)
                set_path(values, {'domain': 'foundation', 'key': 'provider.accounts', 'path': [0, 'price', field]}, invalid)
                result = self.resolve(values)
                self.assertIs(type(result), ManagedConfigurationErr)
                self.assertTrue(any(issue['field'] == 'foundation.provider.accounts[0].price.' + field
                    for issue in self.diagnose(values)['issues']))

    def test_native_cross_field_constraints_are_still_required(self):
        values = self.values()
        fill_external_fields(values)
        set_path(values, {'domain': 'text', 'key': 'provider.transport', 'path': ['roles', 1, 'account_ref']}, 'wrong_account')
        self.assertIs(type(self.resolve(values)), ManagedConfigurationErr)
        values = self.values()
        fill_external_fields(values)
        set_path(values, {'domain': 'foundation', 'key': 'provider.accounts', 'path': [0, 'attempt_limit']}, 17)
        self.assertIs(type(self.resolve(values)), ManagedConfigurationErr)

    def test_form_and_drafts_are_independent_and_do_not_overwrite_explicit_values(self):
        values = self.values()
        fill_external_fields(values)
        before = deepcopy(values)
        view = form_view(registries(self.settings, self.directories, 'default_platform'),
            bootstrap_snapshot(self.settings, self.directories), str(self.root))
        self.assertEqual(values, before)
        fields = view['setup']['fields']
        self.assertEqual(len(fields), 15)
        self.assertEqual(view['setup']['basic_defaults']['role_name'], 'Iris')
        view['setup']['fields'][0]['path'].clear()
        self.assertTrue(cast(list[dict[str, object]], setup_presentation()['fields'])[0]['path'])
        domain = values['foundation']
        assert type(domain) is dict
        domain['provider.role_profiles']['LEARNING'].clear()
        self.assertEqual(self.values()['foundation'], initial_values(self.settings, self.directories, 'Asia/Shanghai')['foundation'])



class LegacyPriceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_price_metadata_recovers_but_new_initialization_requires_evidence(self):
        from companion_memory.persistence import Committed, Ready
        from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
        from companion_memory.runtime.managed_business import ManagedBusiness
        from .test_business import synthetic_resources

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = resolve_deployment({'deployment.data_root': temporary})
            for reopen in (False, True):
                bootstrap = ManagedBootstrap(settings)
                business = None
                try:
                    self.assertIs(type(await bootstrap.open()), Ready)
                    identity = bootstrap.assembly.identity
                    assert identity is not None and bootstrap.resources is not None
                    business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                    if not reopen:
                        values = initial_values(settings, bootstrap.resources.protected_directories(), 'Asia/Shanghai')
                        fill_external_fields(values)
                        for field, legacy in (('source_url', ''), ('checked_date', 'legacy')):
                            set_path(values, {'domain': 'foundation', 'key': 'provider.accounts',
                                'path': [0, 'price', field]}, legacy)
                        draft = {'role_name': '合成恢复角色', 'initial_material': '合成旧版本初始材料。',
                            'timezone': 'Asia/Shanghai', 'timezone_confirmed': True, 'configuration': values}
                        self.assertIs(type(await identity.save_draft('legacy-draft', None, draft)), Committed)
                        refused = await business.initialize('new-submission', 1)
                        assert type(refused) is dict
                        self.assertEqual(refused['state'], 'CONFIGURATION_REQUIRED')
                        self.assertIsNone(business.host)
                        # Represent an accepted older writer's persisted INITIALIZING
                        # decision; recovery must retain those exact original values.
                        self.assertIs(type(await identity.advance_wizard('legacy-initialize', 2,
                            'INITIALIZING', 'legacy-initialize')), Committed)
                    recovered = await business.recover()
                    assert type(recovered) is dict
                    self.assertEqual(recovered['state'], 'AWAITING_REVIEW')
                    self.assertEqual(recovered['startup_sends'], 0)
                    self.assertFalse(business.sends_enabled)
                    confirmed = await business.initialize('legacy-initialize', 2)
                    assert type(confirmed) is dict
                    self.assertEqual(confirmed['state'], 'AWAITING_REVIEW')
                finally:
                    if business is not None:
                        self.assertTrue(await business.close())
                    if bootstrap.assembly.identity is not None:
                        bootstrap.assembly.identity.close()
                    self.assertTrue(await bootstrap.close())


if __name__ == '__main__':
    unittest.main()
