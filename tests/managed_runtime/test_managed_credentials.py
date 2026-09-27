"""Private key publication, original-operation binding and resource isolation."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider.managed_credentials import ManagedCredentials, key_bytes
from companion_memory.runtime.managed_resources import ManagedResources


class ManagedCredentialTests(unittest.TestCase):
    def test_legacy_web_prefix_is_preserved_but_managed_references_never_fall_back(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, private = root / 'legacy', root / 'private'
            legacy.mkdir(mode=0o700)
            private.mkdir(mode=0o700)
            # Exercise the actual two credential readers without acquiring a
            # database owner; physical legacy mount validation belongs to startup.
            resources = object.__new__(ManagedResources)
            resources.secret_root = legacy
            resources.settings = resolve_deployment({'deployment.data_root': str(root / 'data'),
                'deployment.secret_root': str(legacy), 'deployment.provider_secret_root': str(private)})
            resources.identity = {'instance_id': 'synthetic-instance'}
            original = b'legacy-synthetic-credential-0123456789'
            legacy_key = legacy / 'web_deepseek__v1'
            legacy_key.write_bytes(original)
            legacy_key.chmod(0o600)
            self.assertTrue(resources.provider_secret_configured('web_deepseek', 'v1'))
            self.assertEqual(resources.read_provider_secret('web_deepseek', 'v1'), original)
            store = resources.provider_credentials()
            reference = store.reference('original', 'generation')
            shadow = legacy / (reference + '__v1')
            shadow.write_bytes(original)
            shadow.chmod(0o600)
            self.assertFalse(resources.provider_secret_configured(reference, 'v1'))
            with self.assertRaises(OSError):
                resources.read_provider_secret(reference, 'v1')
            store.prepare('original', {}, {}, {'generation': b'new-synthetic-key'})
            self.assertTrue(resources.provider_secret_configured(reference, 'v1'))
            self.assertEqual(resources.read_provider_secret(reference, 'v1'), b'new-synthetic-key')

    def test_exact_private_keys_and_original_bundle_survive_reopen(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ManagedCredentials(root, 'instance')
            request: dict[str, object] = {'role_name': 'Iris'}
            draft: dict[str, object] = {'configuration': {'reference_only': True}}
            supplied = {'generation': b'synthetic-key-A', 'embedding': b'synthetic-key-B'}
            prepared = store.prepare('original', request, draft, supplied)
            directory = root / store.operation_id('original')
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            for path in directory.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('synthetic-key', (directory / 'operation.json').read_text())
            reopened = ManagedCredentials(root, 'instance')
            self.assertEqual(reopened.prepared('original'), prepared)
            for provider, value in supplied.items():
                reference = reopened.reference('original', provider)
                self.assertTrue(reopened.configured(reference, 'v1'))
                self.assertEqual(reopened.read(reference, 'v1'), value)
            self.assertEqual(reopened.prepare('original', request, {'ignored_new_defaults': True}, supplied), prepared)
            self.assertEqual(len(list(root.iterdir())), 1)
            with self.assertRaises(OwnerFailure) as mismatch:
                reopened.prepare('original', request, draft, {'generation': b'changed-key'})
            self.assertEqual(mismatch.exception.reason, 'CONTENT_MISMATCH')
            self.assertNotIn('changed-key', str(mismatch.exception))
            with self.assertRaises(OwnerFailure):
                reopened.prepare('original', {'role_name': 'changed'}, draft, supplied)

    def test_limits_and_unsafe_files_fail_without_disclosing_bytes(self):
        for invalid in ('', 'key with space', 'key\nheader', '中文', 'x' * 4097):
            with self.assertRaises(OwnerFailure):
                key_bytes(invalid, 'generation_api_key')
        self.assertEqual(key_bytes('x', 'generation_api_key'), b'x')
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ManagedCredentials(root, 'instance')
            store.prepare('original', {}, {}, {'generation': b'synthetic-key'})
            reference = store.reference('original', 'generation')
            key = root / store.operation_id('original') / 'generation'
            key.chmod(0o640)
            self.assertFalse(store.configured(reference, 'v1'))
            with self.assertRaises(ValueError):
                store.read(reference, 'v1')
            key.chmod(0o600)
            alias = key.with_name('alias')
            os.link(key, alias)
            self.assertFalse(store.configured(reference, 'v1'))
            alias.unlink()
            key.unlink()
            key.symlink_to(root / 'other')
            with self.assertRaises(OSError):
                store.read(reference, 'v1')
            with self.assertRaises(ValueError):
                store.read('../../outside', 'v1')
            with self.assertRaises(OwnerFailure):
                store.prepare('escape', {}, {}, {'../outside': b'synthetic-key'})

    def test_publish_sync_failure_retains_exact_original_for_retry(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ManagedCredentials(root, 'instance')
            original_fsync = os.fsync
            calls = 0
            def fail_final_sync(descriptor):
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise OSError('synthetic sync failure')
                original_fsync(descriptor)
            with patch('companion_memory.provider.managed_credentials.os.fsync', fail_final_sync):
                with self.assertRaises(OwnerFailure):
                    store.prepare('original', {'expected_revision': None}, {}, {'generation': b'synthetic-key'})
            self.assertEqual(len(list(root.iterdir())), 1)
            with patch('companion_memory.provider.managed_credentials.os.fsync', wraps=original_fsync) as repeated_sync:
                result = store.prepare('original', {'expected_revision': None}, {}, {'generation': b'synthetic-key'})
                self.assertEqual(repeated_sync.call_count, 2)
            self.assertEqual(result['request'], {'expected_revision': None})

    def test_missing_store_is_optional_and_overlap_is_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ManagedCredentials(root / 'absent', 'instance')
            self.assertFalse(store.available())
            self.assertIsNone(store.prepared('original'))
            with self.assertRaises(OwnerFailure) as unavailable:
                store.prepare('original', {}, {}, {'generation': b'synthetic-key'})
            self.assertEqual(unavailable.exception.reason, 'CREDENTIAL_STORAGE_UNAVAILABLE')
            for secret_root in (str(root), str(root / 'nested'), '/run/secrets/nested'):
                with self.assertRaises(ValueError):
                    resolve_deployment({'deployment.data_root': str(root), 'deployment.provider_secret_root': secret_root})


if __name__ == '__main__':
    unittest.main()
