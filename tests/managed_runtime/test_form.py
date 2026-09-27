"""Actual complete registry projection and incomplete-draft rejection."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.configuration.managed_bootstrap import bootstrap_snapshot
from companion_memory.configuration.managed_form import form_view
from companion_memory.configuration.managed_registry import DEFAULT_PLATFORM_ID, initial_values, registries, resolve_values
from companion_memory.configuration.managed_resolution import ManagedConfigurationOk
from .test_business import setup_draft


class FormTests(unittest.TestCase):
    def test_initial_defaults_wait_for_operator_material_and_are_independent(self):
        from copy import deepcopy
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = resolve_deployment({'deployment.data_root': temporary})
            directories = {'media': (str(root / 'blobs'), str(root / 'upload_staging')),
                'database': (str(root / 'db'),), 'audit': (str(root / 'db'),),
                'provider_usage': (str(root / 'db'),), 'backup': (str(root / 'backups'), str(root / 'backup_staging'))}
            values = initial_values(settings, directories, 'Asia/Shanghai')
            text_values = values['text']
            assert type(text_values) is dict
            self.assertTrue(text_values['self_model.initial_persona']['supervision_prompt'].strip())
            self.assertEqual(text_values['self_model.initial_persona']['generation_goal'],
                '维持稳定的发言风格，并充分认识自我。')
            self.assertIsNot(type(resolve_values(settings, directories, DEFAULT_PLATFORM_ID, values)), ManagedConfigurationOk)
            expected = deepcopy(values)
            text_values['cognition.tool_policy']['names'].clear()
            self.assertEqual(initial_values(settings, directories, 'Asia/Shanghai'), expected)

    def test_nested_default_changes_do_not_change_another_draft(self):
        from companion_memory.configuration.managed_defaults import complete_initial
        first = complete_initial('text', 'cognition.tool_policy', {}, '/data')
        second = complete_initial('text', 'cognition.tool_policy', {}, '/data')
        assert type(first) is dict and type(second) is dict
        before = list(second['names'])
        try:
            first['names'].clear()
            self.assertEqual(second['names'], before)
        finally:
            # Preserve process isolation even when demonstrating a shared default bug.
            if not second['names']:
                second['names'].extend(before)

    def test_initial_defaults_resolve_after_only_missing_external_material_is_supplied(self):
        from copy import deepcopy
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = resolve_deployment({'deployment.data_root': temporary})
            directories = {'media': (str(root / 'blobs'), str(root / 'upload_staging')),
                'database': (str(root / 'db'),), 'audit': (str(root / 'db'),),
                'provider_usage': (str(root / 'db'),), 'backup': (str(root / 'backups'), str(root / 'backup_staging'))}
            values = initial_values(settings, directories, 'Asia/Shanghai')
            from .test_setup_defaults import fill_external_fields
            fill_external_fields(values)
            result = resolve_values(settings, directories, DEFAULT_PLATFORM_ID, values)
            self.assertIs(type(result), ManagedConfigurationOk, result)

    def test_all_native_parameters_are_editable_without_fabricating_a_candidate(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = resolve_deployment({'deployment.data_root': temporary})
            directories = {'media': (str(root / 'blobs'), str(root / 'upload_staging')),
                'database': (str(root / 'db'),), 'audit': (str(root / 'db'),),
                'provider_usage': (str(root / 'db'),), 'backup': (str(root / 'backups'), str(root / 'backup_staging'))}
            native = registries(settings, directories, 'sample_platform')
            view = form_view(native, bootstrap_snapshot(settings, directories), temporary)
            self.assertFalse(view['complete'])
            self.assertFalse(view['business_ready'])
            encoded = json.dumps(view, ensure_ascii=False).encode()
            self.assertLess(len(encoded), settings.integer('management.body_max_bytes'))
            scaffold: dict[str, object] = {domain: {entry['key']: entry['initial'] for entry in entries} for domain, entries in view['domains'].items()}
            result = resolve_values(settings, directories, 'sample_platform', scaffold)
            self.assertIsNot(type(result), ManagedConfigurationOk)
            actual = setup_draft(root)['configuration']
            self.assertIs(type(resolve_values(settings, directories, 'sample_platform', actual)), ManagedConfigurationOk)
            for domain, registry in native.items():
                values = scaffold[domain]
                assert type(values) is dict
                self.assertEqual(set(values), {definition.key for definition in registry.list_definitions()})
            self.assertNotIn('synthetic', encoded.decode())


if __name__ == '__main__':
    unittest.main()
