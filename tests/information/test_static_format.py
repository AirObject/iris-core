"""Real large static carrier compatibility with unchanged ordinary command limits.

Repeated configuration declarations are explicit synthetic capacity fixtures;
this test does not represent the complete business host command inventory.
"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from companion_memory.configuration.information_persistence import InformationConfigurationAssembly
from companion_memory.persistence import PersistenceService, DatabaseResources, Ready, Field, RecordSchema, SequenceSchema
from companion_memory.persistence._codec import assembly_value, valid_assembly_encoding
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge
from tests.information.configuration_support import candidate


def large_assembly():
    """Exercise a carrier above one MiB without changing an individual command."""
    assembly = InformationConfigurationAssembly()
    commands = tuple(replace(assembly.commands[0], operation_kind='fixture_initialize_' + str(i)) for i in range(320))
    return assembly.repositories, commands


class StaticFormatTests(unittest.TestCase):
    def test_extended_format_is_explicit_and_legacy_limit_remains(self):
        repositories, commands = large_assembly()
        with self.assertRaises(ValueTooLarge):
            assembly_value(repositories, commands)
        encoded = assembly_value(repositories, commands, assembly_format='LOCAL_INFORMATION_V1')
        self.assertGreater(len(encoded), 1048576)
        self.assertLessEqual(len(encoded), 2760704)
        self.assertTrue(valid_assembly_encoding(encoded, 'LOCAL_INFORMATION_V1'))
        self.assertFalse(valid_assembly_encoding(encoded, 'LEGACY'))
        self.assertFalse(valid_assembly_encoding(b'{"commands":[],"commands":[]}', 'LOCAL_INFORMATION_V1'))
        with self.assertRaises((ValueTooLarge, InvalidValue)):
            assembly_value(repositories, commands + tuple(replace(command, operation_kind=command.operation_kind + '_extra') for command in commands)
                           + commands[:32], assembly_format='LOCAL_INFORMATION_V1')
        print({'synthetic_command_count': len(commands), 'static_carrier_bytes': len(encoded)})

    def test_empty_targets_declaration_is_rejected_at_service_construction(self):
        assembly = InformationConfigurationAssembly()
        original = assembly.commands[0]
        targets = next(field for field in original.result_schema.fields if field.name == 'targets')
        if type(targets.schema) is not SequenceSchema:
            self.fail('A sequence declaration is required for audit targets.')
        invalid = replace(original, result_schema=RecordSchema(tuple(
            replace(field, schema=replace(targets.schema, minimum=0)) if field.name == 'targets' else field
            for field in original.result_schema.fields)))
        with self.assertRaises(InvalidValue):
            PersistenceService(assembly.repositories, (invalid,), assembly_format='LOCAL_INFORMATION_V1')
        PersistenceService(assembly.repositories, assembly.commands, assembly_format='LOCAL_INFORMATION_V1')


class StaticFormatStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_carrier_is_saved_and_checked_by_real_sqlite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            configuration, _ = candidate(root)
            repositories, commands = large_assembly()
            resources = DatabaseResources('large-static', lambda identity, path: identity == 'large-static' and path == str(root / 'database/runtime.sqlite3'))
            for mode in ('CREATE_NEW', 'OPEN_EXISTING'):
                service = PersistenceService(repositories, commands, assembly_format='LOCAL_INFORMATION_V1')
                try:
                    result = await service.initialize(configuration.foundation, resources, mode)
                    self.assertIs(type(result), Ready, result)
                finally:
                    await service.close()

    async def test_format_mismatch_is_rejected_in_both_directions_without_migration(self):
        for first, other in (('LEGACY', 'LOCAL_INFORMATION_V1'), ('LOCAL_INFORMATION_V1', 'LEGACY')):
            with self.subTest(first=first), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                configuration, _ = candidate(root)
                assembly = InformationConfigurationAssembly()
                from companion_memory.persistence._codec import AssemblyFormat
                resources = DatabaseResources('format-bound', lambda identity, path: identity == 'format-bound' and path == str(root / 'database/runtime.sqlite3'))
                first_service = PersistenceService(assembly.repositories, assembly.commands, assembly_format=cast(AssemblyFormat, first))
                try:
                    self.assertIs(type(await first_service.initialize(configuration.foundation, resources, 'CREATE_NEW')), Ready)
                finally:
                    await first_service.close()
                rejected = PersistenceService(assembly.repositories, assembly.commands, assembly_format=cast(AssemblyFormat, other))
                try:
                    self.assertIsNot(type(await rejected.initialize(configuration.foundation, resources, 'OPEN_EXISTING')), Ready)
                finally:
                    await rejected.close()
                reopened = PersistenceService(assembly.repositories, assembly.commands, assembly_format=cast(AssemblyFormat, first))
                try:
                    self.assertIs(type(await reopened.initialize(configuration.foundation, resources, 'OPEN_EXISTING')), Ready)
                finally:
                    await reopened.close()
