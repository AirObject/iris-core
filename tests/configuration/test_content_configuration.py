"""Complete content configuration admission, encoding and persistent identity checks."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import cast
import unittest

from companion_memory.configuration import MetadataValue
from companion_memory.configuration.content_resolution import (
    ContentConfigurationErr, ContentConfigurationOk, resolve_content_configuration, content_snapshot_issue,
)
from companion_memory.configuration.content_codec import candidate_values, encode_content_entry, decode_content_entry
from companion_memory.configuration.content_persistence import ContentConfigurationAssembly
from companion_memory.configuration.content_persistent_results import ConfigurationCommitted
from companion_memory.configuration.persistent_codec import encode_entry, decode_entry
from companion_memory.configuration.snapshots import SnapshotEntry
from companion_memory.persistence import PersistenceService, DatabaseResources, Ready, ResultBoundCommand
from companion_memory.persistence._codec import command_descriptor, prepare_command
from companion_memory.persistence.results import OperationIdentity
from companion_memory.persistence.schema import encode_value, freeze_value, Value
from tests.configuration.content_support import candidate, inputs


class ContentConfigurationTests(unittest.TestCase):
    def test_recommended_complete_configuration_and_full_window(self):
        with TemporaryDirectory() as directory:
            value, _ = candidate(Path(directory))
            self.assertIsNone(content_snapshot_issue(value))
            self.assertEqual(value.material_contracts[0].window_bound(4, 2048, 2, 2048), 40180)
            payload = candidate_values(value)
            domains = cast(list[dict[str, object]], payload['domains'])
            self.assertEqual(len(domains), 4)
            self.assertEqual(sum(len(cast(list, domain['entries'])) for domain in domains), 105)
            assembly = ContentConfigurationAssembly()
            definition = assembly.commands[0]
            values = freeze_value(definition.input_schema, payload)
            encoded = encode_value(MappingProxyType({'definition': command_descriptor(definition), 'values': values,
                'intentions': MappingProxyType({'configuration_initialized': MappingProxyType({'actor': 'bootstrap'})})}), 1048576)
            self.assertLessEqual(len(encoded), 933888)
            prepare_command(definition, OperationIdentity('database', 'configuration', 'initialize_content', 'instance', 'key'),
                            ResultBoundCommand(1, payload, {'configuration_initialized': {'actor': 'bootstrap'}}), 1048576)

    def test_insufficient_fixed_refusal_record_and_total_deadline_rejected(self):
        with TemporaryDirectory() as directory:
            for changes, field, reason in (
                ({'media.interpretation_record_max_bytes': 1330}, 'value', 'RANGE_INVALID'),
                ({'media.preparation_total_timeout_ms': 599999}, 'content', 'BUDGET_INVALID'),
                ({'media.file_worker_capacity': 4}, 'content', 'BUDGET_INVALID'),
                ({'media.interpretation_text_max_bytes': True}, 'value', 'RANGE_INVALID'),
            ):
                with self.subTest(changes=changes):
                    result = resolve_content_configuration(*inputs(Path(directory), cast(dict[str, MetadataValue], changes)))
                    self.assertIs(type(result), ContentConfigurationErr)
                    assert type(result) is ContentConfigurationErr
                    self.assertEqual((result.error.field, result.error.reason), (field, reason))

    def test_controls_del_and_unicode_have_lossless_bounded_envelope_expansion(self):
        with TemporaryDirectory() as directory:
            value, _ = candidate(Path(directory))
            entry = value.content.list_entries()[0]
            for text in ('\x00\b\n\r\t\x1f\x7f', 'é', '😀', '\\n', '"'):
                with self.subTest(text=text):
                    selected = SnapshotEntry(replace(entry.definition, description=text), entry.state)
                    body = encode_content_entry(selected)
                    self.assertNotIn('\x7f', body)
                    self.assertNotIn('\n', body)
                    definition, _, _, _ = decode_content_entry(body)
                    self.assertEqual(definition['description'], text)
                    self.assertLessEqual(len(encode_value(body, 65536)) - 2, 3 * len(body.encode('utf-8')))
                    legacy = encode_entry(selected)
                    self.assertEqual(decode_entry(legacy)[0]['description'], text)
                    with self.assertRaises(ValueError):
                        decode_content_entry(legacy)
                    with self.assertRaises(ValueError):
                        decode_entry(body)

    def test_complete_configuration_aggregate_boundary_is_enforced_before_storage(self):
        from tests.configuration.content_support import maximum_inputs
        from tests.runtime.configuration_support import registry
        with TemporaryDirectory() as directory:
            supplied, declarations = maximum_inputs(Path(directory))
            result = resolve_content_configuration(*supplied)
            self.assertIs(type(result), ContentConfigurationOk, result)
            assert type(result) is ContentConfigurationOk
            payload = candidate_values(result.value)
            domains = cast(list[dict[str, object]], payload['domains'])
            self.assertEqual(sum(len(cast(list, domain['entries'])) for domain in domains), 128)
            self.assertEqual(sum(len(entry['body'].encode('utf-8')) for domain in domains
                                 for entry in cast(list[dict[str, str]], domain['entries'])), 262144)
            declaration = declarations[-1]
            declaration['description'] += 'x'
            cast(dict[str, object], supplied[3])['registry'] = registry(declarations)
            rejected = resolve_content_configuration(*supplied)
            self.assertIs(type(rejected), ContentConfigurationErr)
            assert type(rejected) is ContentConfigurationErr
            self.assertEqual((rejected.error.field, rejected.error.reason), ('content', 'CAPACITY_INSUFFICIENT'))

    def test_new_configuration_is_not_a_legacy_candidate(self):
        from companion_memory.configuration.runtime_resolution import runtime_snapshot_issue, resolve_runtime_configuration
        with TemporaryDirectory() as directory:
            value, supplied = candidate(Path(directory))
            self.assertIsNotNone(runtime_snapshot_issue(value))
            self.assertNotIsInstance(resolve_runtime_configuration(supplied[0], supplied[1], supplied[2], supplied[4], supplied[5]), ContentConfigurationOk)


class ContentConfigurationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_maximum_configuration_writes_and_reads_complete_values(self):
        from tests.configuration.content_support import maximum_inputs
        with TemporaryDirectory() as directory:
            supplied, _ = maximum_inputs(Path(directory))
            resolved = resolve_content_configuration(*supplied)
            assert type(resolved) is ContentConfigurationOk, resolved
            value = resolved.value
            assembly = ContentConfigurationAssembly()
            storage = PersistenceService(assembly.repositories, assembly.commands)
            root = Path(directory).resolve()
            resources = DatabaseResources('content-maximum', lambda identity, path: identity == 'content-maximum' and path == str(root / 'database' / 'runtime.sqlite3'))
            self.assertIs(type(await storage.initialize(value.foundation, resources, 'CREATE_NEW')), Ready)
            binding = assembly.bind(storage, 'instance')
            try:
                result = await binding.persist_content_configuration('maximum-config', value, actor='bootstrap', protected_directories=supplied[4])
                self.assertIs(type(result), ConfigurationCommitted, result)
                assert type(result) is ConfigurationCommitted
                self.assertIsNotNone(result.configuration, result)
                assert result.configuration is not None
                self.assertEqual(candidate_values(result.configuration.candidate), candidate_values(value))
            finally:
                binding.close()
                await storage.close()
                binding.close()

    async def test_real_sqlite_close_reopen_preserves_identity_and_original_receipt(self):
        with TemporaryDirectory() as directory:
            value, supplied = candidate(Path(directory))
            original = None
            for mode in ('CREATE_NEW', 'OPEN_EXISTING'):
                assembly = ContentConfigurationAssembly()
                storage = PersistenceService(assembly.repositories, assembly.commands)
                resources = DatabaseResources('content-database', lambda identity, path: identity == 'content-database' and path == str(Path(directory).resolve() / 'database' / 'runtime.sqlite3'))
                ready = await storage.initialize(value.foundation, resources, mode)
                self.assertIs(type(ready), Ready, ready)
                binding = assembly.bind(storage, 'instance')
                try:
                    result = await binding.persist_content_configuration('original-config', value, actor='bootstrap', protected_directories=supplied[4])
                    self.assertIs(type(result), ConfigurationCommitted, result)
                    assert type(result) is ConfigurationCommitted
                    self.assertIsNotNone(result.configuration, result)
                    assert result.configuration is not None
                    if original is None:
                        original = result.receipt
                    else:
                        self.assertEqual(result.receipt, original)
                        self.assertEqual(result.source, 'EXISTING')
                    self.assertEqual(result.configuration.candidate.content.integer('media.interpretation_record_max_bytes'), 2048)
                    self.assertEqual(candidate_values(result.configuration.candidate), candidate_values(value))
                finally:
                    binding.close()
                    await storage.close()
                    binding.close()
