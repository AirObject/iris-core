"""No implicit DeepSeek allowance, shared fourteen slots, no extras or replay.

Approvals here are explicitly synthetic and live only in disposable test roots.
The original MiniMax authorization is neither imported nor modified by tests.
"""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from .authorization import AuthorizationJournal
from .test_controls import GOOD


def allowance():
    return {'package_digest':'a'*64,'code_digest':'b'*64,'approved_by':'synthetic-human','approval_ref':'synthetic-decision',
        'billing_policy':'DEEPSEEK_TOKEN_METERED_TRIAL','cost_limit_atoms':30000000,'quota_limit':0,
        'per_attempt_money_bound':2113537,'per_attempt_quota_bound':0,'extra_operations':{},
        'operations':[f'{p}:{o}' for p in ('macos','linux') for o in ('persona',*(f'learn-{i}' for i in range(6)))],
        'independent_trial':{'new_attempts':14,'cost_limit_atoms':30000000,'old_attempts':10,'historical_maximum':24,
            'old_remaining_slots_transferable':False,'old_remote_state':'REMOTE_RESULT_UNKNOWN','old_cleanup_ended':True,
            'old_stop_evidence_digest':'c'*64,'old_package_digest':'d'*64,'new_account_id':'deepseek-trial-account',
            'new_instances':{p:'deepseek-trial-'+p for p in ('macos','linux')},
            'new_database_ids':{p:'deepseek-text-trial-'+p for p in ('macos','linux')},
            'new_roots':{p:'/synthetic-deepseek/'+p for p in ('macos','linux')},
            'count_approved':True,'money_approved':True,'independent_under_old_unknown_approved':True,'user_decision_ref':'synthetic-decision'}}


class AuthorizationTests(unittest.TestCase):
    def test_no_inferred_approval_and_no_old_slot_transfer(self):
        for field,value in (('count_approved',None),('money_approved',False),('independent_under_old_unknown_approved',None),
                ('old_remote_state','FAILED'),('old_remaining_slots_transferable',True),('old_attempts',0),('historical_maximum',30)):
            with self.subTest(field=field),TemporaryDirectory() as directory:
                a=allowance();a['independent_trial'][field]=value
                journal=AuthorizationJournal(Path(directory)/'authorization')
                with self.assertRaises(ValueError):journal.create(a)
                self.assertFalse(journal.path.exists())

    def test_fourteen_shared_reservations_no_extra_and_no_restart_reset(self):
        with TemporaryDirectory() as directory:
            journal=AuthorizationJournal(Path(directory)/'authorization');a=allowance();journal.create(a)
            with self.assertRaises(ValueError):journal.approve_extra('macos:retry','Synthetic retry','synthetic')
            now=100000000000
            for operation in a['operations']:
                with patch('time.time_ns',return_value=now):
                    token=journal.reserve(operation,'a'*64,'b'*64,GOOD)
                    with self.assertRaises(ValueError):journal.reserve('linux:persona','a'*64,'b'*64,GOOD)
                    self.assertTrue(journal.settle(token,evidence_digest='e'*64,attempts=1,money=2113537,quota=0,
                        remote_known=True,local_committed=True,cleanup_ended=True,cost_complete=True,integrity_ok=True,usage_complete=True))
                journal=AuthorizationJournal(journal.path);now+=31000000000
            self.assertEqual(sum(e['kind']=='RESERVED' for e in journal.inspect()['events']),14)
            for operation in ('macos:persona','linux:learn-5','macos:persona-generation-2'):
                with patch('time.time_ns',return_value=now),self.assertRaises(ValueError):journal.reserve(operation,'a'*64,'b'*64,GOOD)
            with self.assertRaises(FileExistsError):journal.create(a)

    def test_unknown_retains_liability_and_stops_following_platform(self):
        with TemporaryDirectory() as directory:
            journal=AuthorizationJournal(Path(directory)/'authorization');journal.create(allowance())
            token=journal.reserve('macos:persona','a'*64,'b'*64,GOOD)
            self.assertFalse(journal.settle(token,evidence_digest='e'*64,attempts=1,money=0,quota=0,
                remote_known=False,local_committed=False,cleanup_ended=True,cost_complete=False,integrity_ok=True))
            self.assertEqual(journal.inspect()['events'][-1]['money_responsibility'],2113537)
            with self.assertRaises(ValueError):AuthorizationJournal(journal.path).reserve('linux:persona','a'*64,'b'*64,GOOD)
