"""Managed format reuses the actual daily host with explicit synthetic reviewers."""
from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest

from companion_memory.configuration.definitions import ParameterDefinition
from companion_memory.configuration.persistent_codec import _decode, _encode
from companion_memory.configuration.daily_resolution import freeze_daily_domains
from companion_memory.configuration.managed_resolution import resolve_managed_configuration, ManagedConfigurationOk, bind_managed_material, PROTECTED_KEYS
from companion_memory.configuration.managed_schema import material_definitions
from companion_memory.runtime.daily_host import DailyCognitionHost
from companion_memory.persistence import Found, Committed
from tests.dream_maintenance.configuration_support import inputs
from tests.dream_maintenance.host_support import make_dream_host


def managed_inputs(root):
    supplied: list[Any] = list(inputs(root))
    domains = {'foundation': supplied[0], 'runtime': supplied[1], 'platform': supplied[2][0],
               'content': supplied[3], 'information': supplied[4], 'text': supplied[5]}
    definitions = {}
    for name, domain in domains.items():
        definitions[name] = []
        for definition in domain['registry'].list_definitions():
            raw = {f.name: _decode(_encode(getattr(definition, f.name))) for f in fields(ParameterDefinition)}
            if raw['key'] in PROTECTED_KEYS:
                raw['sensitivity'] = 'administrator'
            if raw['key'] == 'provider.profiles':
                raw['apply_mode'] = 'NEXT_REQUEST'
            definitions[name].append(raw)
    definitions['text'] = list(material_definitions())
    registries = freeze_daily_domains(definitions)
    for name, domain in domains.items():
        domain['registry'] = registries[name]
    supplied[7] = [bind_managed_material(supplied[2][0]['platform_id'])]
    return supplied


class NativeHostTests(unittest.IsolatedAsyncioTestCase):
    def test_managed_schedule_and_budgets_are_independent_of_trial_package(self):
        with TemporaryDirectory() as directory:
            values = managed_inputs(Path(directory))
            text_values = values[5]['explicit_values']
            schedule = text_values['dream.schedule']
            schedule.update(local_time='07:35', focus_default=False, enabled=False)
            text_values['memory.long_term_maintenance']['decay_enabled'] = False
            for account in values[0]['explicit_values']['provider.accounts']:
                account['attempt_limit'] = 3
            valid = resolve_managed_configuration(*values)
            self.assertIs(type(valid), ManagedConfigurationOk, valid)
            schedule['local_time'] = '24:00'
            invalid = resolve_managed_configuration(*values)
            self.assertIsNot(type(invalid), ManagedConfigurationOk)
            schedule['local_time'] = '07:35'
            text_values['dream.schedule']['unexpected'] = True
            self.assertIsNot(type(resolve_managed_configuration(*values)), ManagedConfigurationOk)

    def test_native_registry_accepts_only_complete_startup_bound_values(self):
        from companion_memory.configuration.deployment import resolve_deployment
        from companion_memory.configuration.managed_registry import resolve_values
        from companion_memory.configuration.managed_bootstrap import STORAGE_DEFAULTS
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = managed_inputs(root)
            values = {name: raw[index]['explicit_values'] for name, index in
                (('foundation', 0), ('runtime', 1), ('content', 3), ('information', 4), ('text', 5))}
            values['platform'] = raw[2][0]['explicit_values']
            values['foundation'].update(STORAGE_DEFAULTS)
            values['foundation'].update({'storage.database_file': str(root / 'db/memory.sqlite3'),
                'logging.file_directory': str(root / 'logs/runtime')})
            values['content'].update({'media.root_directory': str(root / 'blobs'),
                'media.staging_directory': str(root / 'upload_staging')})
            directories = {'media': (str(root / 'blobs'), str(root / 'upload_staging')),
                'database': (str(root / 'db'),), 'audit': (str(root / 'db'),),
                'provider_usage': (str(root / 'db'),), 'backup': (str(root / 'backups'),)}
            result = resolve_values(resolve_deployment({'deployment.data_root': str(root)}), directories, 'sample_platform', values)
            self.assertIs(type(result), ManagedConfigurationOk, result)

    async def test_new_managed_host_and_original_reopen_have_zero_sends(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ('CREATE_NEW', 'OPEN_EXISTING'):
                legacy_resources = make_dream_host(root, 9, []).resources
                parsed = resolve_managed_configuration(*managed_inputs(root))
                self.assertIs(type(parsed), ManagedConfigurationOk, parsed)
                assert type(parsed) is ManagedConfigurationOk
                host = DailyCognitionHost(parsed.value, replace(legacy_resources,
                    version_transports=lambda candidate: legacy_resources.transports))
                host.configure_entry('entry', 'partition', ('self',), ({'kind': 'REAL', 'context_id': None},))
                try:
                    opened = await host.initialize(mode)
                    self.assertIs(type(opened), Found, opened)
                    assert type(opened) is Found
                    self.assertEqual(opened.value['startup_sends'], 0)
                    if mode == 'CREATE_NEW':
                        registered = await host.register_entry('entry', 'entry', 'host', 'sample_platform', 'conversation')
                        self.assertIs(type(registered), Committed, registered)
                finally:
                    self.assertTrue(await host.close())


if __name__ == '__main__':
    unittest.main()
