"""Complete runtime domains preserve fixed first errors and immutable provenance."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from companion_memory.configuration import RuntimeConfigurationOk,RuntimeConfigurationErr,resolve_runtime_configuration,NoDefault,MetadataValue
from companion_memory.configuration.runtime_resolution import runtime_snapshot_issue
from companion_memory.configuration.runtime_schema import RUNTIME_REQUIREMENTS
from tests.runtime.configuration_support import inputs,definitions,registry


class Hostile:
    def __getattribute__(self,name):raise AssertionError('A rejected object was inspected.')
    def __eq__(self,other):raise AssertionError('A rejected object was compared.')
    def __repr__(self):raise AssertionError('A rejected object was formatted.')


class RuntimeResolutionTests(unittest.TestCase):
    def test_complete_values_are_deeply_isolated_and_definitions_have_no_defaults(self):
        with TemporaryDirectory(prefix='iris-full-config-') as root:
            source=inputs(Path(root));result=resolve_runtime_configuration(*source)
            assert type(result) is RuntimeConfigurationOk,result
            before=result.value.runtime.integer('ingress.event_max_bytes')
            cast(dict,source[1]['explicit_values'])['ingress.event_max_bytes']=1
            cast(dict,source[0]['explicit_values'])['provider.profiles'][0]['model_id']='changed'
            self.assertEqual(result.value.runtime.integer('ingress.event_max_bytes'),before)
            self.assertIsNone(runtime_snapshot_issue(result.value))
            self.assertTrue(all(type(e.definition.default) is NoDefault for e in result.value.runtime.list_entries()))
            with self.assertRaises((AttributeError,TypeError)):setattr(result.value,'platforms',())
            with self.assertRaises((AttributeError,TypeError)):setattr(result.value.material_contracts[0],'version',2)

    def test_key_safety_precedes_unknown_keys_across_domains(self):
        with TemporaryDirectory(prefix='iris-config-first-error-') as root:
            source=inputs(Path(root));cast(dict,source[0]['explicit_values'])['unknown.key']=1
            cast(dict,source[1]['explicit_values'])['invalid key']=Hostile()
            failure=resolve_runtime_configuration(*source);assert type(failure) is RuntimeConfigurationErr
            self.assertEqual((failure.error.code,failure.error.field,failure.error.reason),('INVALID_INPUT','runtime','INVALID_SHAPE'))
            self.assertNotIn('invalid key',str(failure));self.assertNotIn('unknown.key',str(failure))

    def test_all_capabilities_precede_definitions_and_values(self):
        with TemporaryDirectory(prefix='iris-config-capability-') as root:
            source=inputs(Path(root));items=definitions(RUNTIME_REQUIREMENTS)
            items[0]['validator']=['unregistered_validator']
            source[1]['registry']=registry(items)
            cast(dict,source[0]['explicit_values'])['provider.accounts']=cast(MetadataValue,Hostile())
            failure=resolve_runtime_configuration(*source);assert type(failure) is RuntimeConfigurationErr
            self.assertEqual((failure.error.code,failure.error.reason),('UNSUPPORTED_CAPABILITY','VALIDATOR_UNSUPPORTED'))

    def test_required_metadata_and_full_window_fail_without_partial_publication(self):
        cases=({'ingress.event_max_bytes':True},{'learning.material_max_bytes':1000},{'learning.output_units_limit':1025},
               {'management.observation_row_limit':1},{'runtime.max_active_entries':8})
        with TemporaryDirectory(prefix='iris-config-relations-') as root:
            for changes in cases:
                with self.subTest(changes=changes):
                    source=inputs(Path(root),runtime_changes=cast(dict[str,MetadataValue],changes))
                    failure=resolve_runtime_configuration(*source);self.assertIs(type(failure),RuntimeConfigurationErr)
            source=inputs(Path(root));items=definitions(RUNTIME_REQUIREMENTS);items[0]['default']=NoDefault()
            items[0]['owner_module']='another_owner';source[1]['registry']=registry(items)
            failure=resolve_runtime_configuration(*source);assert type(failure) is RuntimeConfigurationErr
            self.assertEqual((failure.error.code,failure.error.reason),('DEFINITION_MISMATCH','METADATA_MISMATCH'))

    def test_material_versions_and_platform_set_cannot_be_substituted(self):
        with TemporaryDirectory(prefix='iris-config-material-') as root:
            source=inputs(Path(root));material=source[4][0]
            fake=object.__new__(type(material))
            for field in ('platform_id','participant','event_format','material_format','template','bound_rule','version'):
                object.__setattr__(fake,field,getattr(material,field))
            object.__setattr__(fake,'version',2);source[4][0]=fake
            failure=resolve_runtime_configuration(*source);assert type(failure) is RuntimeConfigurationErr
            self.assertEqual((failure.error.code,failure.error.reason),('VALUE_INVALID','BUDGET_INVALID'))
            object.__setattr__(fake,'template',Hostile())
            failure=resolve_runtime_configuration(*source);assert type(failure) is RuntimeConfigurationErr
            self.assertEqual(failure.error.reason,'BUDGET_INVALID')
