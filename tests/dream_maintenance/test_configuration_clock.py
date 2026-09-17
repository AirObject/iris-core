"""Exercise strict new configuration and exact civil/absolute time boundaries."""
import tempfile
import unittest
from datetime import date,datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from companion_memory.configuration.daily_resolution import resolve_daily_configuration,DailyConfigurationErr
from companion_memory.configuration.dream_resolution import resolve_dream_configuration,DreamConfigurationErr as DreamErr
from companion_memory.configuration.dream_codec import candidate_values
from companion_memory.persistence.schema import InvalidValue
from companion_memory.runtime.dream_clock import due_date,scheduled_instant,utc_microseconds,settle_decay,DAY_US
from .configuration_support import candidate,inputs


class ConfigurationClockTests(unittest.TestCase):
    def test_complete_new_configuration_and_old_format_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            value,supplied=candidate(Path(directory))
            encoded=candidate_values(value)
            self.assertEqual(sum(len(domain['entries']) for domain in encoded['domains']),136)
            self.assertEqual(len(encoded['domains']),6)
            self.assertLessEqual(sum(len(e['body'].encode()) for d in encoded['domains'] for e in d['entries']),524288)
            self.assertIs(type(resolve_daily_configuration(*supplied)),DailyConfigurationErr)

    def test_material_capacity_and_boolean_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            supplied=inputs(Path(directory)); supplied[0]['explicit_values']['provider.profiles'][-1]['max_input_units']=262144
            self.assertIs(type(resolve_dream_configuration(*supplied)),DreamErr)
            supplied=inputs(Path(directory)); supplied[5]['explicit_values']['dream.schedule']['enabled']=1
            self.assertIs(type(resolve_dream_configuration(*supplied)),DreamErr)

    def test_dst_gap_and_repeated_hour_choose_first_valid_instant(self):
        zone=ZoneInfo('America/New_York')
        self.assertEqual(scheduled_instant(date(2026,3,8),zone,'02:30'),datetime(2026,3,8,7,tzinfo=timezone.utc))
        self.assertEqual(scheduled_instant(date(2026,11,1),zone,'01:30'),datetime(2026,11,1,5,30,tzinfo=timezone.utc))

    def test_missed_dates_collapse_and_duplicate_tick_is_empty(self):
        now=utc_microseconds(datetime(2026,9,16,4,tzinfo=timezone.utc))
        selected=due_date(now,ZoneInfo('UTC'),'03:00','2026-09-01')
        self.assertIsNotNone(selected)
        if selected is None:raise AssertionError('Expected one due date')
        self.assertEqual(selected.local_date,'2026-09-16')
        self.assertIsNone(due_date(now,ZoneInfo('UTC'),'03:00',selected.local_date))
        self.assertIsNone(due_date(now-DAY_US,ZoneInfo('UTC'),'03:00',selected.local_date))

    def test_decay_boundary_debt_saturation_and_run_cap(self):
        args=dict(retention=5,accounted_until=DAY_US,interval_seconds=86400,decrement=1,max_intervals=7,already_applied_in_run=0)
        self.assertEqual(settle_decay(**args,now_us=2*DAY_US-1).applied_intervals,0)
        result=settle_decay(**args,now_us=21*DAY_US)
        self.assertEqual((result.retention,result.accounted_until,result.remaining_intervals),(0,8*DAY_US,13))
        again=settle_decay(**{**args,'retention':result.retention,'accounted_until':result.accounted_until,'already_applied_in_run':7},now_us=21*DAY_US)
        self.assertEqual(again.applied_intervals,0)
        self.assertEqual(again.remaining_intervals,13)
        regressed=settle_decay(**args,now_us=DAY_US-1)
        self.assertTrue(regressed.clock_regressed)
        self.assertEqual(regressed.accounted_until,DAY_US)
        with self.assertRaises(InvalidValue):settle_decay(**{**args,'retention':True},now_us=DAY_US)
