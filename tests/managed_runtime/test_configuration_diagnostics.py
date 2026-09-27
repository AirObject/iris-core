"""Incomplete real draft diagnostics preserve full resolution and send gates.

Fixtures supply explicit synthetic operator material only to positive cases.
Pure schema diagnostics reveal declared locations, never submitted values. The
application case uses actual identity persistence without starting a listener.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.configuration.managed_bootstrap import bootstrap_snapshot
from companion_memory.configuration.managed_diagnostics import (
    ISSUE_LIMIT, _Collector, diagnose_managed_configuration,
)
from companion_memory.configuration.managed_form import form_view
from companion_memory.configuration.managed_registry import initial_values, registries, resolve_values
from companion_memory.configuration.managed_resolution import ManagedConfigurationErr, ManagedConfigurationOk
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.persistence import Committed, Ready
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import Field, RecordSchema, ScalarSchema
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from .test_business import setup_draft


def directories(root: Path) -> dict[str, tuple[str, ...]]:
    return {'media': (str(root / 'blobs'), str(root / 'upload_staging')),
        'database': (str(root / 'db'),), 'audit': (str(root / 'db'),),
        'provider_usage': (str(root / 'db'),), 'backup': (str(root / 'backups'), str(root / 'backup_staging'))}


class ConfigurationDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = resolve_deployment({'deployment.data_root': self.temporary.name})
        self.directories = directories(self.root)

    def diagnose(self, values):
        return diagnose_managed_configuration(self.settings, self.directories, 'sample_platform', values)

    def resolve(self, values):
        return resolve_values(self.settings, self.directories, 'sample_platform', values)

    def test_real_initial_defaults_report_bounded_missing_locations_without_mutation(self):
        values = initial_values(self.settings, self.directories, 'Asia/Shanghai', 'sample_platform')
        before = deepcopy(values)
        result = self.diagnose(values)
        self.assertGreater(len(result['issues']), 0)
        self.assertLess(len(result['issues']), ISSUE_LIMIT)
        self.assertFalse(result['issues_truncated'])
        self.assertIn({'field': 'foundation.provider.accounts[0].evidence_ref',
            'reason': 'MISSING_REQUIRED'}, result['issues'])
        self.assertEqual(values, before)
        self.assertIs(type(self.resolve(values)), ManagedConfigurationErr)
        self.assertLess(len(json.dumps(result).encode()), 16384)

    def test_unchanged_form_roundtrip_reports_empty_string_identifiers(self):
        values = cast(dict[str, dict[str, object]], initial_values(
            self.settings, self.directories, 'Asia/Shanghai', 'sample_platform'))
        view = form_view(registries(self.settings, self.directories, 'sample_platform'),
            bootstrap_snapshot(self.settings, self.directories), str(self.root))

        def browser_value(schema, value):
            # Text inputs return an empty string; numeric/select inputs keep null.
            if schema['type'] == 'object':
                return {field['name']: None if field.get('nullable') and value[field['name']] is None else
                    browser_value(field['schema'], value[field['name']]) for field in schema['fields']
                    if field['name'] in value}
            if schema['type'] == 'array':
                return [browser_value(schema['items'][index] if 'items' in schema else schema['item'], item)
                    for index, item in enumerate(value)]
            return '' if value is None and schema['type'] == 'string' and not schema.get('choices') else value

        saved = {domain: {entry['key']: browser_value(entry['schema'], values[domain][entry['key']])
            for entry in entries} for domain, entries in view['domains'].items()}
        accounts = saved['foundation']['provider.accounts']
        assert type(accounts) is list
        account = accounts[0]
        assert type(account) is dict
        self.assertEqual(account['evidence_ref'], '')
        self.assertIs(type(account['price']), dict)
        self.assertIn({'field': 'foundation.provider.accounts[0].evidence_ref', 'reason': 'MISSING_REQUIRED'},
            self.diagnose(saved)['issues'])
        self.assertIs(type(self.resolve(saved)), ManagedConfigurationErr)

    def test_native_nullable_optional_false_and_zero_are_not_missing(self):
        schema = RecordSchema((Field('optional', ScalarSchema('identifier'), optional=True),
            Field('nullable', ScalarSchema('identifier'), nullable=True),
            Field('enabled', ScalarSchema('boolean')), Field('count', ScalarSchema('integer', 0, 8))))
        collector = _Collector()
        collector.inspect(schema, {'nullable': None, 'enabled': False, 'count': 0}, 'known')
        self.assertEqual(collector.issues, [])
        self.assertFalse(collector.truncated)
        collector.inspect(schema, {'nullable': None, 'enabled': False}, 'known')
        self.assertEqual(collector.issues, [{'field': 'known.count', 'reason': 'MISSING_REQUIRED'}])

    def test_native_types_identifiers_utf8_and_array_bounds_do_not_echo_values(self):
        for group, key, index, field, value, reason in (
            ('foundation', 'provider.accounts', 0, 'account_id', ' ', 'MISSING_REQUIRED'),
            ('foundation', 'provider.accounts', 0, 'account_id', 'private / credential', 'INVALID_IDENTIFIER'),
            ('foundation', 'provider.accounts', 0, 'attempt_limit', 0, 'OUT_OF_RANGE'),
            ('foundation', 'provider.accounts', 0, 'attempt_limit', True, 'TYPE_MISMATCH'),
            ('foundation', 'provider.accounts', 0, 'billing_mode', 'private-billing', 'NOT_IN_ENUM'),
            ('text', 'self_model.initial_persona', None, 'supervision_prompt', '界' * 343, 'TEXT_TOO_LONG'),
            ('text', 'self_model.initial_persona', None, 'supervision_prompt', '\ud800', 'INVALID_TEXT'),
        ):
            with self.subTest(field=field, reason=reason):
                values = setup_draft(self.root)['configuration']
                owner = values[group][key] if index is None else values[group][key][index]
                owner[field] = value
                expected = f'{group}.{key}' + ('' if index is None else f'[{index}]') + '.' + field
                result = self.diagnose(values)
                self.assertIn({'field': expected, 'reason': reason}, result['issues'])
                self.assertNotIn('private', json.dumps(result))
                self.assertIs(type(self.resolve(values)), ManagedConfigurationErr)
        for count in (0, 10, 10000):
            values = setup_draft(self.root)['configuration']
            values['foundation']['provider.profiles'] = [None] * count
            result = self.diagnose(values)
            self.assertIn({'field': 'foundation.provider.profiles', 'reason': 'ARRAY_LENGTH_INVALID'}, result['issues'])
            self.assertNotIn('foundation.provider.profiles[', json.dumps(result))

    def test_unknown_keys_and_budget_exhaustion_are_safe_and_explicit(self):
        values = setup_draft(self.root)['configuration']
        values['private-unknown-name'] = {'secret': 'private-value'}
        values['text']['self_model.initial_persona']['private-field'] = 'private-value'
        result = self.diagnose(values)
        self.assertIn({'field': 'configuration', 'reason': 'UNKNOWN_FIELD'}, result['issues'])
        self.assertIn({'field': 'text.self_model.initial_persona', 'reason': 'UNKNOWN_FIELD'}, result['issues'])
        self.assertNotIn('private', json.dumps(result))
        with patch('companion_memory.configuration.managed_diagnostics.NODE_LIMIT', 1):
            bounded = self.diagnose(setup_draft(self.root)['configuration'])
        self.assertTrue(bounded['issues_truncated'])
        self.assertEqual(bounded['issues'], [])

    def test_blank_persona_instructions_are_rejected_by_complete_resolution(self):
        for field in ('generation_goal', 'supervision_prompt'):
            for value in ('', ' \t\n', None):
                with self.subTest(field=field, blank=value):
                    values = setup_draft(self.root)['configuration']
                    values['text']['self_model.initial_persona'][field] = value
                    result = self.resolve(values)
                    self.assertIs(type(result), ManagedConfigurationErr)
                    if value is not None:
                        assert type(result) is ManagedConfigurationErr
                        self.assertEqual(result.error.field, 'self_model.initial_persona.' + field)
                        self.assertEqual(result.error.reason, 'MISSING_REQUIRED')
                    self.assertIn({'field': 'text.self_model.initial_persona.' + field, 'reason': 'MISSING_REQUIRED'},
                        self.diagnose(values)['issues'])

    def test_empty_diagnostics_do_not_override_cross_field_failure_or_approve_defaults(self):
        values = setup_draft(self.root)['configuration']
        self.assertIs(type(self.resolve(values)), ManagedConfigurationOk)
        self.assertEqual(self.diagnose(values), {'issues': [], 'issues_truncated': False})
        values['foundation']['provider.role_profiles']['LEARNING'] = ['unbound-profile']
        self.assertEqual(self.diagnose(values), {'issues': [], 'issues_truncated': False})
        self.assertIs(type(self.resolve(values)), ManagedConfigurationErr)


class WizardDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_waiting_draft_validation_reports_issues_without_changing_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': directory}))
            app = None
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                app = ManagedApplication(bootstrap)
                identity = app.identity
                self.assertIs(type(await identity.establish('diagnostic-admin',
                    app.resources.read_secret('bootstrap').decode(), 'synthetic-diagnostic-password')), Committed)
                _, session = await identity.login('diagnostic-login', 'synthetic-diagnostic-password')
                assert session is not None
                principal = await identity.authenticate(session['session'], host=False)
                draft = {'role_name': '合成诊断角色', 'initial_material': '这是无模型请求的测试材料。',
                    'timezone': 'Asia/Shanghai', 'timezone_confirmed': True, 'platform_id': 'sample_platform'}
                saved = await app.dispatch(principal, 'POST', '/api/wizard/save',
                    {'key': 'diagnostic-draft', 'expected_revision': None, 'draft': draft})
                self.assertIs(type(saved), Committed)
                confirmed = await app.business.initialize('diagnostic-confirm', 1)
                self.assertEqual(confirmed, {'state': 'CONFIGURATION_REQUIRED', 'business_ready': False, 'startup_sends': 0})
                before = await identity.read_draft()
                self.assertEqual(before['revision'], 2)
                result = await app.dispatch(principal, 'POST', '/api/wizard/validate', {'expected_revision': 2})
                assert type(result) is dict
                self.assertFalse(result['valid'])
                self.assertFalse(result['issues_truncated'])
                self.assertIn({'field': 'text.provider.embedding_transport.secret_ref', 'reason': 'MISSING_REQUIRED'}, result['issues'])
                self.assertIn({'field': 'text.provider.transport.roles[0].secret_ref', 'reason': 'MISSING_REQUIRED'}, result['issues'])
                self.assertTrue(all(issue['field'].endswith('.secret_ref') for issue in result['issues']))
                original = resolve_values(bootstrap.settings, app.resources.protected_directories(),
                    'sample_platform', cast(dict[str, object], cast(dict[str, object], before['draft'])['configuration']))
                assert type(original) is ManagedConfigurationErr
                self.assertEqual(result['error'], original.error)
                with self.assertRaises(OwnerFailure) as denied:
                    await app.dispatch(principal, 'POST', '/api/wizard/initialize',
                        {'key': 'diagnostic-incomplete-start', 'expected_revision': 2})
                self.assertEqual(denied.exception.reason, 'CONFIGURATION_INVALID')
                self.assertEqual(await identity.read_draft(), before)
                self.assertFalse(app.business.initialized)
                self.assertFalse(app.business.sends_enabled)
                supplied = setup_draft(root)['configuration']
                for key, values, expected_valid in (
                    ('diagnostic-complete', supplied, True),
                    ('diagnostic-cross-field', {**supplied, 'foundation': {**supplied['foundation'],
                        'provider.role_profiles': {**supplied['foundation']['provider.role_profiles'],
                            'LEARNING': ['unbound-profile']}}}, False),
                    ('diagnostic-resource', {**supplied, 'foundation': {**supplied['foundation'],
                        'storage.database_file': str(root / 'other.sqlite3')}}, False),
                ):
                    current = await identity.read_draft()
                    self.assertIs(type(await identity.save_draft(key, cast(int, current['revision']),
                        {**cast(dict[str, object], current['draft']), 'configuration': values})), Committed)
                    snapshot = await identity.read_draft()
                    checked = await app.dispatch(principal, 'POST', '/api/wizard/validate',
                        {'expected_revision': snapshot['revision']})
                    assert type(checked) is dict
                    self.assertIs(checked['valid'], expected_valid)
                    if not expected_valid:
                        self.assertEqual(checked['issues'], [])
                        self.assertFalse(checked['issues_truncated'])
                    self.assertEqual(await identity.read_draft(), snapshot)
                    self.assertFalse(app.business.initialized)
                    self.assertFalse(app.business.sends_enabled)
            finally:
                if app is not None:
                    self.assertTrue(await app.business.close())
                    app.identity.close()
                self.assertTrue(await bootstrap.close())



class WaitingHealthTests(unittest.TestCase):
    def test_health_separates_configuration_waiting_from_unavailable_service(self):
        from companion_memory.runtime.managed_cli import health
        for state, status, expected in (
            ('CONFIGURATION_REQUIRED', 200, 0),
            ('BOOTSTRAP', 200, 0),
            ('AWAITING_REVIEW', 200, 0),
            ('READY', 200, 0),
            ('UNKNOWN_STATE', 200, 1),
            ('CONFIGURATION_REQUIRED', 503, 1),
        ):
            with self.subTest(state=state, status=status):
                payload = {'state': state, 'business_ready': state == 'READY'}
                with patch('companion_memory.runtime.managed_cli.http.client.HTTPConnection') as factory:
                    connection = factory.return_value
                    response = connection.getresponse.return_value
                    response.status = status
                    response.read.return_value = json.dumps({'health': payload}).encode()
                    with redirect_stdout(StringIO()) as output:
                        self.assertEqual(health(), expected)
                    self.assertEqual(json.loads(output.getvalue()), payload)
                    response.read.assert_called_once_with(8193)
                    connection.close.assert_called_once()

if __name__ == '__main__':
    unittest.main()
