"""Full configuration encoding, strict values and real SQLite original-key identity."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.configuration.information_resolution import (
    InformationConfigurationErr, InformationConfigurationOk, resolve_information_configuration, information_snapshot_issue,
)
from companion_memory.configuration.content_resolution import content_snapshot_issue
from companion_memory.configuration.information_schema import information_definitions, SUPPORTED_VALUES
from companion_memory.configuration.information_codec import candidate_values
from companion_memory.configuration.information_persistence import InformationConfigurationAssembly
from companion_memory.configuration.information_persistent_results import ConfigurationCommitted
from companion_memory.persistence import PersistenceService, DatabaseResources, Ready, ResultBoundCommand
from companion_memory.persistence._codec import command_descriptor, prepare_command, assembly_value
from companion_memory.persistence.results import OperationIdentity
from companion_memory.persistence.schema import encode_value, freeze_value
from tests.information.configuration_support import candidate, inputs
from tests.runtime.configuration_support import registry


class InformationConfigurationTests(unittest.TestCase):
    def test_complete_definitions_values_and_intentions_fit_the_real_command(self):
        with TemporaryDirectory() as directory:
            value, _ = candidate(Path(directory))
            self.assertIsNone(information_snapshot_issue(value))
            self.assertIsNotNone(content_snapshot_issue(value))
            payload = candidate_values(value)
            domains = cast(list[dict[str, object]], payload['domains'])
            self.assertEqual(len(domains), 5)
            self.assertEqual(sum(len(cast(list[object], d['entries'])) for d in domains), 113)
            sizes = [len(e['body'].encode()) for d in domains for e in cast(list[dict[str, str]], d['entries'])]
            self.assertLessEqual(sum(sizes), 294912)
            definition = InformationConfigurationAssembly().commands[0]
            encoded = encode_value(MappingProxyType({'definition': command_descriptor(definition),
                'values': freeze_value(definition.input_schema, payload),
                'intentions': MappingProxyType({'configuration_initialized': MappingProxyType({'actor': 'bootstrap'})})}), 1048576)
            self.assertLessEqual(len(encoded), 1007616)
            prepare_command(definition, OperationIdentity('database', 'configuration', 'initialize_information', 'instance', 'key'),
                ResultBoundCommand(1, payload, {'configuration_initialized': {'actor': 'bootstrap'}}), 1048576)
            print({'configuration_entries': len(sizes), 'body_bytes': sum(sizes), 'initialization_bytes': len(encoded)})

    def test_each_nested_field_rejects_wrong_type_missing_extra_and_unsupported_value(self):
        with TemporaryDirectory() as directory:
            supplied = inputs(Path(directory))
            domain = supplied[4]
            original = domain['explicit_values']
            for key, record in SUPPORTED_VALUES.items():
                for field, value in record.items():
                    alternatives: tuple[object, ...] = (None, str(value) if type(value) is not str else 1,
                        not value if type(value) is bool else value + 1 if type(value) is int else 'UNSUPPORTED')
                    for invalid in alternatives:
                        with self.subTest(key=key, field=field, invalid=invalid):
                            domain['explicit_values'] = {**original, key: {**record, field: invalid}}
                            self.assertIs(type(resolve_information_configuration(*supplied)), InformationConfigurationErr)
                    domain['explicit_values'] = {**original, key: {k: v for k, v in record.items() if k != field}}
                    self.assertIs(type(resolve_information_configuration(*supplied)), InformationConfigurationErr)
                domain['explicit_values'] = {**original, key: {**record, 'unknown': 1}}
                self.assertIs(type(resolve_information_configuration(*supplied)), InformationConfigurationErr)
            domain['explicit_values'] = original
            self.assertIs(type(resolve_information_configuration(*supplied)), InformationConfigurationOk)

    def test_metadata_is_exact_and_values_are_deeply_immutable(self):
        with TemporaryDirectory() as directory:
            value, supplied = candidate(Path(directory))
            declarations = list(information_definitions())
            declarations[0] = {**declarations[0], 'description': 'Unapproved metadata.'}
            supplied[4]['registry'] = registry(declarations)
            self.assertIs(type(resolve_information_configuration(*supplied)), InformationConfigurationErr)
            with self.assertRaises(TypeError):
                cast(dict[str, object], value.information.record('retrieval.local'))['mode'] = 'OTHER'
            self.assertEqual(value.information.record('retrieval.local')['mode'], 'LOCAL_LEXICAL_V1')


class InformationConfigurationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_configuration_reopens_with_original_receipt_and_canonical_values(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            value, supplied = candidate(root)
            original = None
            for mode in ('CREATE_NEW', 'OPEN_EXISTING'):
                assembly = InformationConfigurationAssembly()
                storage = PersistenceService(assembly.repositories, assembly.commands, assembly_format='LOCAL_INFORMATION_V1')
                resource = DatabaseResources('information-database', lambda identity, path: identity == 'information-database' and path == str(root / 'database' / 'runtime.sqlite3'))
                self.assertIs(type(await storage.initialize(value.foundation, resource, mode)), Ready)
                binding = assembly.bind(storage, 'instance')
                try:
                    result = await binding.persist_information_configuration('original-config', value, actor='bootstrap', protected_directories=supplied[5])
                    self.assertIs(type(result), ConfigurationCommitted, result)
                    if type(result) is not ConfigurationCommitted or result.configuration is None:
                        self.fail(repr(result))
                    self.assertEqual(candidate_values(result.configuration.candidate), candidate_values(value))
                    if original is None:
                        original = result.receipt
                    else:
                        self.assertEqual(result.receipt, original)
                        self.assertEqual(result.source, 'EXISTING')
                finally:
                    binding.close()
                    await storage.close()
                    binding.close()
