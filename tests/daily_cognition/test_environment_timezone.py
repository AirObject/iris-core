"""Environment defaults become explicit immutable configuration, never time rewrites."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
from companion_memory.configuration.daily_preparation import prepare_daily_configuration
from companion_memory.configuration.daily_resolution import DailyConfigurationOk, DailyConfigurationErr
from companion_memory.configuration.daily_codec import candidate_inputs
from companion_memory.persistence import Found
from .configuration_support import inputs
from .test_host import make_host


class EnvironmentTimezoneTests(unittest.IsolatedAsyncioTestCase):
    def test_process_iana_defaults_explicit_override_and_fixed_error(self):
        with TemporaryDirectory() as directory:
            supplied = inputs(Path(directory))
            del supplied[5]['explicit_values']['runtime.timezone']
            for zone in ('Europe/Paris', 'America/New_York'):
                with patch.dict(os.environ, {'TZ': zone}):
                    result = prepare_daily_configuration(*supplied)
                self.assertIs(type(result), DailyConfigurationOk, result)
                assert type(result) is DailyConfigurationOk
                self.assertEqual(result.value.text.value('runtime.timezone'), zone)
                self.assertNotIn('runtime.timezone', supplied[5]['explicit_values'])
            for zone in ('', 'Not/AZone', ':GMT+8', '/etc/localtime'):
                with patch.dict(os.environ, {'TZ': zone}):
                    result = prepare_daily_configuration(*supplied)
                self.assertIs(type(result), DailyConfigurationErr)
                assert type(result) is DailyConfigurationErr
                self.assertEqual(result.error.reason, 'EXPLICIT_CONFIGURATION_REQUIRED')
            with patch.dict(os.environ, {}, clear=True), patch('companion_memory.configuration.daily_preparation.Path.is_symlink', return_value=False), patch('companion_memory.configuration.daily_preparation.Path.is_file', return_value=False):
                result=prepare_daily_configuration(*supplied)
                assert type(result) is DailyConfigurationErr
                self.assertEqual(result.error.reason, 'EXPLICIT_CONFIGURATION_REQUIRED')
            supplied[5]['explicit_values']['runtime.timezone'] = 'Asia/Tokyo'
            with patch.dict(os.environ, {'TZ': 'invalid'}):
                result=prepare_daily_configuration(*supplied)
                assert type(result) is DailyConfigurationOk
                self.assertEqual(result.value.text.value('runtime.timezone'), 'Asia/Tokyo')

    def test_absolute_offset_deadline_never_reapplies_environment_timezone(self):
        from types import SimpleNamespace,MappingProxyType
        from companion_memory.cognition.daily_candidates import DailyCandidateTransform
        from companion_memory.persistence.schema import InvalidValue
        payloads={'message':SimpleNamespace(event={'body':'本次展示计划在2030-01-02T10:00:00+08:00开始。'})}
        anchor=MappingProxyType({'message_id':'message','part':'BODY','item_index':None,'start_utf8':None,'end_utf8':None})
        from datetime import datetime
        absolute=int(datetime.fromisoformat('2030-01-02T10:00:00+08:00').timestamp())*1000000
        for zone in ('Europe/Paris','America/New_York'):
            with patch.dict(os.environ,{'TZ':zone}):
                DailyCandidateTransform._deadline({'deadline':absolute,'target_anchors':(anchor,)},payloads)
                with self.assertRaises(InvalidValue):
                    DailyCandidateTransform._deadline({'deadline':absolute+8*3600*1000000,'target_anchors':(anchor,)},payloads)

    async def test_persistent_reopen_uses_original_explicit_zone(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); supplied = inputs(root)
            del supplied[5]['explicit_values']['runtime.timezone']
            with patch.dict(os.environ, {'TZ': 'Europe/Paris'}):
                result = prepare_daily_configuration(*supplied)
            self.assertIs(type(result), DailyConfigurationOk)
            assert type(result) is DailyConfigurationOk
            persisted_inputs = candidate_inputs(result.value, supplied[6])
            credentials = []
            for mode, zone in (('CREATE_NEW', 'Europe/Paris'), ('OPEN_EXISTING', 'America/New_York')):
                with patch.dict(os.environ, {'TZ': zone}):
                    host = make_host(root, 9, credentials, configuration_input=persisted_inputs)
                    try:
                        self.assertIs(type(await host.initialize(mode)), Found)
                        assert host.stored is not None
                        self.assertEqual(host.stored.candidate.text.value('runtime.timezone'), 'Europe/Paris')
                        self.assertFalse(credentials)
                    finally:
                        self.assertTrue(await host.close())
