"""Actual complete registry projection and incomplete-draft rejection."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.configuration.managed_bootstrap import bootstrap_snapshot
from companion_memory.configuration.managed_form import form_view
from companion_memory.configuration.managed_registry import registries, resolve_values
from companion_memory.configuration.managed_resolution import ManagedConfigurationOk
from .test_business import setup_draft


class FormTests(unittest.TestCase):
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
