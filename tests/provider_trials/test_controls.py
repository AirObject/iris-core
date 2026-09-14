"""Offline qualification controls never inspect user credentials or contact a vendor.

Private temporary files, process restarts and real native storage test the
outer authorization boundary. Synthetic annotations test arithmetic only.
"""
from __future__ import annotations
import copy
import json
import itertools
import os
from pathlib import Path
from typing import cast
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.provider.file_credentials import protected_file_resolver
from companion_memory.provider.credentials import Available,CredentialUnavailable
from .authorization import AuthorizationJournal,Observation,MAX_DIRECTORY_BYTES,MIN_FREE_BYTES,MAX_OPERATIONS
from .files import canonical,write_new
from .materials import GOLD,CASES,corpus
from .prepare import prepare
from .preflight import inspect
from .review import annotation_template,score


def allowance() -> dict:
    return {'package_digest':'a'*64,'code_digest':'b'*64,'approved_by':'synthetic-reviewer','approval_ref':'deterministic-fixture',
        'cost_limit_atoms':160,'quota_limit':32,'per_attempt_money_bound':10,'per_attempt_quota_bound':2,
        'operations':[f'{p}:{s}' for p in ('macos','linux') for s in ('persona',*(f'learn-{i}' for i in range(6)))],
        'extra_operations':{'macos:persona-retry':'Explicit synthetic known-failure retry','linux:diagnostic':'Explicit synthetic diagnostic'}}

GOOD=Observation(0,10*1024**3,0,True)


def settle(journal: AuthorizationJournal,token: str,**changes) -> bool:
    return journal.settle(token,**({'evidence_digest':'c'*64,'attempts':1,'money':10,'quota':2,'remote_known':True,
        'local_committed':True,'cleanup_ended':True,'cost_complete':True,'integrity_ok':True}|changes))


class CredentialFileTests(unittest.TestCase):
    def test_binding_and_wrong_reference_never_open_the_file(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);path=root/'credential';path.write_bytes(b'synthetic-only\n');path.chmod(0o600)
            with patch('os.open',side_effect=AssertionError('Credential read outside native resolve')):
                resolver=protected_file_resolver(path,secret_ref='secret',secret_revision='revision',account_ref='account')
                self.assertEqual(resolver.resolve('wrong','revision','account'),CredentialUnavailable('UNAVAILABLE'))
            result=resolver.resolve('secret','revision','account');self.assertIs(type(result),Available)
            assert type(result) is Available
            self.assertNotIn('synthetic-only',repr(result));self.assertFalse(result.lease.released)
            result.lease.release();self.assertTrue(result.lease.released)

    def test_links_directory_permissions_oversize_and_control_bytes_are_rejected(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);path=root/'credential';path.write_bytes(b'synthetic-only');path.chmod(0o600)
            resolver=protected_file_resolver(path,secret_ref='s',secret_revision='r',account_ref='a')
            for value in (b'',b'a'*4098,b'bad\r\n',b'bad secret',b'bad\x00'):
                path.write_bytes(value);self.assertEqual(resolver.resolve('s','r','a'),CredentialUnavailable('FAILED'))
            path.write_bytes(b'synthetic-only');path.chmod(0o644)
            self.assertEqual(resolver.resolve('s','r','a'),CredentialUnavailable('FAILED'));path.chmod(0o600)
            linked=root/'link';linked.symlink_to(path)
            other=protected_file_resolver(linked,secret_ref='s',secret_revision='r',account_ref='a')
            self.assertEqual(other.resolve('s','r','a'),CredentialUnavailable('FAILED'))
            os.link(path,root/'hardlink')
            self.assertEqual(resolver.resolve('s','r','a'),CredentialUnavailable('FAILED'))
            root.chmod(0o755)
            self.assertEqual(resolver.resolve('s','r','a'),CredentialUnavailable('FAILED'));root.chmod(0o700)


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        clock=itertools.count(100_000_000_000,31_000_000_000)
        timer=patch('tests.provider_trials.authorization.time.time_ns',side_effect=lambda:next(clock))
        timer.start();self.addCleanup(timer.stop)

    def test_two_platforms_and_restarts_share_exactly_sixteen_consumed_slots(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'allowance';journal=AuthorizationJournal(path);approval=allowance();journal.create(approval)
            for operation in (*approval['operations'],*approval['extra_operations']):
                journal=AuthorizationJournal(path)
                token=journal.reserve(operation,'a'*64,'b'*64,GOOD);self.assertTrue(settle(journal,token))
            self.assertEqual(len(journal.inspect()['events']),32)
            with self.assertRaises(ValueError):journal.reserve('linux:persona','a'*64,'b'*64,GOOD)
            with self.assertRaises(FileExistsError):journal.create(approval)

    def test_next_request_waits_thirty_seconds_after_confirmed_cleanup(self):
        with TemporaryDirectory() as directory:
            journal=AuthorizationJournal(Path(directory)/'allowance');journal.create(allowance())
            token=journal.reserve('macos:persona','a'*64,'b'*64,GOOD)
            with patch('tests.provider_trials.authorization.time.time_ns',return_value=100_000_000_000):
                settle(journal,token)
                with self.assertRaisesRegex(ValueError,'interval'):journal.reserve('linux:persona','a'*64,'b'*64,GOOD)
            with patch('tests.provider_trials.authorization.time.time_ns',return_value=130_000_000_000):
                journal.reserve('linux:persona','a'*64,'b'*64,GOOD)

    def test_crashed_parent_reservation_blocks_fresh_interpreter(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'allowance';journal=AuthorizationJournal(path);journal.create(allowance())
            journal.reserve('macos:persona','a'*64,'b'*64,GOOD)
            code="from pathlib import Path;from tests.provider_trials.authorization import AuthorizationJournal,Observation;AuthorizationJournal(Path(__import__('sys').argv[1])).reserve('linux:persona','a'*64,'b'*64,Observation(0,10*1024**3,0,True))"
            child=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True)
            self.assertNotEqual(child.returncode,0);self.assertIn('Unresolved',child.stderr)
            self.assertEqual(len(journal.inspect()['events']),1)

    def test_unknown_billing_cleanup_integrity_and_attempt_overrun_stop(self):
        for change in ({'remote_known':False},{'cost_complete':False},{'cleanup_ended':False},
                       {'local_committed':False},{'integrity_ok':False},{'attempts':2},{'money':11},{'quota':3}):
            with self.subTest(change=change),TemporaryDirectory() as directory:
                journal=AuthorizationJournal(Path(directory)/'allowance');journal.create(allowance())
                token=journal.reserve('macos:persona','a'*64,'b'*64,GOOD)
                self.assertFalse(settle(journal,token,**change))
                with self.assertRaises(ValueError):journal.reserve('linux:persona','a'*64,'b'*64,GOOD)
                if change.get('cost_complete') is False:
                    self.assertEqual(journal.inspect()['events'][-1]['money_responsibility'],10)

    def test_exact_budget_bounds_unapproved_extras_frozen_material_and_resource_limits(self):
        with TemporaryDirectory() as directory:
            journal=AuthorizationJournal(Path(directory)/'allowance');approval=allowance();approval['cost_limit_atoms']=10;journal.create(approval)
            for operation,packet,code in (('macos:unapproved','a'*64,'b'*64),('macos:persona','d'*64,'b'*64),('macos:persona','a'*64,'d'*64)):
                with self.assertRaises(ValueError):journal.reserve(operation,packet,code,GOOD)
            for observation in (Observation(MAX_DIRECTORY_BYTES,GOOD.free_bytes,0,True),Observation(0,MIN_FREE_BYTES,0,True),
                                Observation(0,GOOD.free_bytes,MAX_OPERATIONS,True),Observation(0,GOOD.free_bytes,0,False)):
                with self.assertRaises(ValueError):journal.reserve('macos:persona','a'*64,'b'*64,observation)
            token=journal.reserve('macos:persona','a'*64,'b'*64,GOOD);settle(journal,token)
            with self.assertRaises(ValueError):journal.reserve('linux:persona','a'*64,'b'*64,GOOD)

    def test_missing_event_and_content_corruption_fail_closed(self):
        with TemporaryDirectory() as directory:
            journal=AuthorizationJournal(Path(directory)/'allowance');journal.create(allowance())
            token=journal.reserve('macos:persona','a'*64,'b'*64,GOOD);settle(journal,token)
            path=journal.path/'event-000.json';value=json.loads(path.read_text());value['operation']='altered';path.write_bytes(canonical(value))
            with self.assertRaises(ValueError):journal.inspect()


class ReviewTests(unittest.TestCase):
    def signed(self) -> dict:
        value=annotation_template('macos');value.update(reviewed_by='synthetic-reviewer',review_ref='arithmetic-fixture',package_digest='a'*64)
        for i,batch in enumerate(value['batches']):
            batch.update(terminal='SUCCEEDED',source_syntax_valid=True,world_errors=0,person_errors=0,permission_violations=0,
                         raw_output_ref='synthetic-output',omissions=[],output_propositions=[])
            for item in GOLD:
                if item[1]//2==i:batch['output_propositions'].append({'text':item[2],'object_ref':'synthetic-object','match':item[0],
                    'supported':True,'anchor_supported':True,'duplicate_of':None})
        return value

    def test_no_human_signature_or_missing_annotations_never_pass(self):
        with self.assertRaises(ValueError):score(annotation_template('macos'),package_digest='a'*64)
        value=self.signed();value['batches'][0]['output_propositions']=None
        with self.assertRaises(ValueError):score(value,package_digest='a'*64)
        for field in ('source_syntax_valid','raw_output_ref'):
            value=self.signed();value['batches'][0][field]=None
            with self.assertRaises(ValueError):score(value,package_digest='a'*64)

    def test_unique_recall_duplicate_denominator_empty_set_and_unknown_cover(self):
        value=self.signed();result=score(value,package_digest='a'*64)
        self.assertTrue(result['quality_gate_passed']);self.assertIsNone(result['batches'][4]['precision']);self.assertIsNone(result['batches'][4]['recall'])
        prediction=copy.deepcopy(value['batches'][0]['output_propositions'][0]);prediction['duplicate_of']=0
        value['batches'][0]['output_propositions'].extend([prediction,copy.deepcopy(prediction)])
        result=score(value,package_digest='a'*64);self.assertEqual(result['precision'],10/12);self.assertEqual(result['recall'],1);self.assertFalse(result['quality_gate_passed'])
        value=self.signed();value['batches'][5]['terminal']='UNKNOWN'
        self.assertFalse(score(value,package_digest='a'*64)['quality_gate_passed'])

    def test_syntax_does_not_replace_human_anchor_support_and_omissions_are_exact(self):
        value=self.signed();value['batches'][0]['source_syntax_valid']=False
        result=score(value,package_digest='a'*64)
        self.assertEqual(result['anchor_support'],1)
        self.assertFalse(result['quality_gate_passed'])
        value=self.signed();value['batches'][0]['output_propositions'][0]['anchor_supported']=False
        self.assertFalse(score(value,package_digest='a'*64)['quality_gate_passed'])
        value=self.signed();value['batches'][0]['output_propositions']=[];value['batches'][0]['omissions']=['paper-notes','repotted']
        result=score(value,package_digest='a'*64);self.assertIsNone(result['batches'][0]['precision']);self.assertEqual(result['recall'],0.8)
        value['batches'][0]['omissions']=[]
        with self.assertRaises(ValueError):score(value,package_digest='a'*64)

    def test_complete_package_stays_unsendable_even_with_material_approval(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);checksum=prepare(root/'package')
            value=json.loads((root/'package/package.json').read_text())
            for platform in ('macos','linux'):
                self.assertEqual(sum(len(d['entries']) for d in value['configuration'][platform]['domains']),118)
                self.assertEqual(value['configuration'][platform]['values']['provider.accounts'][0]['billing_mode'],'SUBSCRIPTION')
                self.assertIsNone(value['configuration'][platform]['values']['provider.generation']['model_context_tokens'])
            write_new(root/'approval.json',canonical({'package_digest':checksum,'materials_approved':True,'approved_by':'synthetic-reviewer','approval_ref':'fixture'}))
            report=inspect(root/'package/package.json',root/'approval.json')
            self.assertTrue(report['materials_approved']);self.assertFalse(report['ready_to_send'])
            self.assertEqual(report['provider_requests'],0);self.assertEqual(len(report['blocking_conditions']),3)
            self.assertEqual(len(cast(list,corpus()['messages'])),13)
