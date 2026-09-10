"""Explicit provider schemas, fixed validator failures and old-entry isolation."""
from typing import cast
from unittest.mock import patch
from companion_memory.configuration import (MetadataValue, ProviderResolutionErr, ProviderResolutionOk, ResolutionErr,
    CheckedResolutionErr, PersistenceResolutionErr, provider_snapshot_issue, resolve_configuration,
    resolve_configuration_with_logging_validation, resolve_configuration_with_persistence_validation,
    resolve_configuration_with_provider_validation)
from companion_memory.configuration import provider_schema
from tests.configuration.provider_support import provider_definitions, provider_values
from tests.configuration.resolution_support import ResolutionTestCase
from tests.configuration.logging_support import logging_definitions


class ProviderValidationTests(ResolutionTestCase):
    def resolve(self, changes=None, values=None, omit=(), logs=False):
        definitions=provider_definitions()+(logging_definitions() if logs else [])
        for definition in definitions:
            cast(dict[str,object],definition).update((changes or {}).get(definition["key"],{}))
            definition["dependencies"]=[name for name in definition["dependencies"] if name not in omit]
        registry=self.registry(*(definition for definition in definitions if definition["key"] not in omit))
        explicit=provider_values("/synthetic-public/provider.sqlite3")
        explicit.update(values or {})
        for name in omit:
            explicit.pop(name,None)
        return registry,resolve_configuration_with_provider_validation(registry,explicit,None)

    def test_complete_owned_values_have_no_persisted_revision(self):
        registry,result=self.resolve()
        assert type(result) is ProviderResolutionOk,result
        self.assertEqual(len(result.value.list_entries()),20)
        self.assertIsNone(provider_snapshot_issue(result.value))
        self.assertIs(result.value.get_registry(),registry)
        self.assertFalse(hasattr(result.value,"config_snapshot_id"))

    def test_old_three_entrypoints_continue_rejecting_provider_validators(self):
        registry,result=self.resolve()
        values=provider_values("/synthetic-public/provider.sqlite3")
        self.assertIs(type(resolve_configuration(registry,values)),ResolutionErr)
        self.assertIs(type(resolve_configuration_with_logging_validation(registry,values,{})),CheckedResolutionErr)
        self.assertIs(type(resolve_configuration_with_persistence_validation(registry,values,None)),PersistenceResolutionErr)

    def test_all_required_definitions_and_nested_identity_constraints(self):
        for definition in provider_definitions():
            with self.subTest(key=definition["key"]):
                _,result=self.resolve(omit=(definition["key"],))
                self.assertIs(type(result),ProviderResolutionErr,result)
        for name,value in (("provider.max_in_flight",True),("provider.result_max_bytes",8193),("provider.query_row_limit",0),
                           ("provider.accounts",[]),("provider.profiles",[]),("provider.role_profiles",{"LEARNING":["absent"]})):
            with self.subTest(key=name):
                _,result=self.resolve(values={name:value})
                self.assertIs(type(result),ProviderResolutionErr,result)

    def test_fixed_validator_exceptions_and_illegal_returns_are_safe(self):
        for action in (RuntimeError("private synthetic detail"),"illegal",42,False):
            with self.subTest(action=type(action).__name__):
                with patch.object(provider_schema,"validate",side_effect=action if isinstance(action,Exception) else None,return_value=action):
                    _,result=self.resolve()
                assert type(result) is ProviderResolutionErr,result
                self.assertEqual(result.error.issues[0].reason,"VALIDATOR_FAILED")
                self.assertNotIn("private",repr(result))

    def test_resource_envelope_lower_bound_and_exact_boundary(self):
        for limit,accepted in ((57343,False),(57344,True),(65536,True)):
            _,result=self.resolve(values={"storage.command_max_bytes":limit,"storage.receipt_max_bytes":limit,"provider.result_max_bytes":8192})
            self.assertIs(type(result),ProviderResolutionOk if accepted else ProviderResolutionErr,result)

    def test_applicability_checks_all_definitions_before_missing_capabilities(self):
        definitions=provider_definitions()
        for definition in definitions:
            definition['validator']=[]
            definition['dependencies']=[]
        definitions[-1]['owner_module']='other_owner'
        registry=self.registry(*definitions)
        snapshot=self.resolved(registry,provider_values('/synthetic-public/provider.sqlite3'))
        self.assertEqual(provider_snapshot_issue(snapshot),'DEFINITION_MISMATCH')
        definitions[-1]=provider_definitions()[-1]
        definitions[-1]['validator']=[]
        definitions[-1]['dependencies']=[]
        snapshot=self.resolved(self.registry(*definitions),provider_values('/synthetic-public/provider.sqlite3'))
        self.assertEqual(provider_snapshot_issue(snapshot),'CAPABILITY_MISSING')

    def test_logging_missing_validator_is_capability_failure_after_valid_definitions(self):
        from companion_memory.configuration import provider_resolution
        from tests.configuration.logging_support import protected_directories
        definitions=provider_definitions()+logging_definitions()
        for definition in definitions:
            if definition['key']=='logging.file_directory':definition['validator']=[]
        values=provider_values('/synthetic-public/provider.sqlite3')
        values['logging.file_directory']='/synthetic-public/runtime-logs'
        registry=self.registry(*definitions)
        # Simulate a native snapshot published by an earlier narrower resolver.
        # Applicability must recheck declarations independently of its publisher.
        with patch.object(provider_resolution,'_check_logging_schema',return_value=None):
            result=resolve_configuration_with_provider_validation(registry,values,protected_directories())
        assert type(result) is ProviderResolutionOk,result
        self.assertEqual(provider_snapshot_issue(result.value),'CAPABILITY_MISSING')

    def test_full_logging_context_and_snapshot_value_validation_are_independent(self):
        from companion_memory.configuration import provider_resolution
        from tests.configuration.logging_support import protected_directories
        definitions=provider_definitions()+logging_definitions()
        registry=self.registry(*definitions)
        values=provider_values('/synthetic-public/provider.sqlite3');values['logging.file_directory']='/synthetic-public/runtime-logs'
        for context,accepted in ((None,False),({},False),(protected_directories(),True)):
            result=resolve_configuration_with_provider_validation(registry,values,context)
            self.assertIs(type(result),ProviderResolutionOk if accepted else ProviderResolutionErr,result)
            if type(result) is ProviderResolutionOk:self.assertIsNone(provider_snapshot_issue(result.value))
        values['logging.file_directory']='/synthetic-public/media'
        self.assertIs(type(resolve_configuration_with_provider_validation(registry,values,protected_directories())),ProviderResolutionErr)
        own=self.registry(*provider_definitions())
        invalid=provider_values('/synthetic-public/provider.sqlite3');invalid['provider.role_profiles']={'LEARNING':['absent']}
        with patch.object(provider_resolution,'run_validator',return_value=None):
            result=resolve_configuration_with_provider_validation(own,invalid,None)
        assert type(result) is ProviderResolutionOk,result
        self.assertEqual(provider_snapshot_issue(result.value),'VALUE_INVALID')
