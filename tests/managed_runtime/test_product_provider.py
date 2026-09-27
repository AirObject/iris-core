"""Ordinary usage admission, honest money and exact legacy ledger declarations.

These tests perform no supplier I/O. They exercise the native row validators,
the actual account admission predicate and repository stop-query semantics.
"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.configuration.managed_registry import initial_values, resolve_values
from companion_memory.configuration.managed_resolution import ManagedConfigurationOk
from companion_memory.provider.account_policy import attempt_limit_reached
from companion_memory.provider.daily_stored_schema import validate
from companion_memory.provider.daily_usage import normalize
from companion_memory.provider.deepseek_protocol import observe_usage
from companion_memory.provider.embedding_allocated_usage import observe as embedding_usage
from companion_memory.provider.values import as_record, freeze
from companion_memory.provider.values import plain
from companion_memory.provider.ledger import LedgerAssembly
from companion_memory.provider.text_command_policy import validate_values
from companion_memory.persistence.provider_repository import create_provider_repository
from companion_memory.persistence.schema import InvalidValue, freeze_value
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap


def product_values(root: Path):
    settings=resolve_deployment({'deployment.data_root':str(root)})
    directories={'media':(str(root/'blobs'),str(root/'upload_staging')),
        'database':(str(root/'db'),),'audit':(str(root/'db'),),'provider_usage':(str(root/'db'),),
        'backup':(str(root/'backups'),str(root/'backup_staging'))}
    values=initial_values(settings,directories,'Asia/Shanghai',product=True)
    text=values['text'];assert type(text) is dict
    for transport in text['provider.transport']['roles']:transport['secret_ref']='synthetic-generation'
    text['provider.embedding_transport']['secret_ref']='synthetic-embedding'
    return settings,directories,values


class ProductProviderTests(unittest.TestCase):
    def test_only_provider_credentials_complete_unpriced_default_configuration(self):
        with TemporaryDirectory() as directory:
            settings,directories,values=product_values(Path(directory))
            result=resolve_values(settings,directories,'default_platform',values,require_price_evidence=True)
            self.assertIs(type(result),ManagedConfigurationOk,result)
            foundation=values['foundation'];assert type(foundation) is dict
            for account in foundation['provider.accounts']:
                self.assertEqual(account['billing_mode'],'USAGE_ONLY')
                for field in ('attempt_limit','cost_limit_atoms','price'):self.assertIsNone(account[field])

    def test_more_than_32_attempts_keep_original_unknown_money_and_finite_policy_limits(self):
        with TemporaryDirectory() as directory:
            _,_,values=product_values(Path(directory))
            foundation=values['foundation'];assert type(foundation) is dict
            account=as_record(freeze(foundation['provider.accounts'][0],4096,owned=True))
            profile=as_record(freeze(foundation['provider.profiles'][0],4096,owned=True))
            observed=observe_usage(as_record(freeze({'prompt_tokens':10,'completion_tokens':2,'total_tokens':12,
                'prompt_cache_hit_tokens':2,'prompt_cache_miss_tokens':8},4096,owned=True)))
            assembly=LedgerAssembly(daily_format=True,dream_format=True,managed_format=True,product_format=True)
            definition=next(d for d in assembly.commands if d.operation_kind=='initialize_budget')
            budget:dict[str,object]={}
            for count in range(65):
                self.assertFalse(attempt_limit_reached(account,count))
                budget={'object_id':'budget','revision':count+1,'account_id':account['account_id'],
                    'window_id':account['window_id'],'policy':account,'attempt_count':count,
                    'known_subtotal_atoms':0,'held_atoms':0,'risk_state':'CLEAR','format_version':6,
                    'quota_reserved':0,'quota_known':None,'quota_held':0,'billing_mode':'USAGE_ONLY'}
                self.assertEqual(validate('budget_windows',budget,dream=True,product=True)['attempt_count'],count)
                command=freeze_value(definition.input_schema,{'changes':({'table':'budget_windows','object_id':'budget',
                    'expected_revision':None,'body':json.dumps(plain(as_record(freeze(budget,8192,owned=True)))),
                    'payload':None},),'event':'{}'},owned=True)
                assert type(command) is MappingProxyType
                validate_values(definition,command)
            usage=normalize(observed,account,profile)
            self.assertIsNone(usage['known_cost_atoms']);self.assertFalse(usage['cost_complete'])
            self.assertEqual(as_record(usage['fields'])['input_tokens'],10)
            self.assertIsNone(embedding_usage(b'{"usage":{"prompt_tokens":12,"total_tokens":12}}',billing_mode='USAGE_ONLY')['known_cost_atoms'])
            absent=normalize(observe_usage(None),account,profile)
            self.assertIsNone(absent['known_cost_atoms']);self.assertEqual(absent['coverage'],'UNAVAILABLE')
            no_send=normalize(observed,account,profile,not_sent=True)
            self.assertEqual(no_send['known_cost_atoms'],0);self.assertTrue(no_send['cost_complete'])
            trial=as_record(freeze(dict(account)|{'billing_mode':'USAGE_ONLY_TRIAL','attempt_limit':16},4096,owned=True))
            self.assertFalse(attempt_limit_reached(trial,15));self.assertTrue(attempt_limit_reached(trial,16))
            with self.assertRaises(InvalidValue):validate('budget_windows',budget,dream=True)

    def test_product_query_does_not_hide_unknown_behind_an_earlier_known_embedding_failure(self):
        for product in (False,True):
            repository=create_provider_repository(daily_format=True,dream_format=True,product_format=product)
            sql=dict(repository.statements)['requests_blocked'].sql
            with sqlite3.connect(':memory:') as db:
                db.execute('CREATE TABLE provider_requests(scope_id TEXT,object_id TEXT,revision INT,body TEXT,caller_scope TEXT,caller_module TEXT,extension_id TEXT,operation_key TEXT)')
                for key,phase,outcome in (('a-known','TERMINAL','FAILED'),('z-unknown','REMOTE_RESULT_UNKNOWN',None)):
                    body={'account_id':'embedding','capability':'EMBEDDING','phase':phase,'outcome':outcome,
                        'execution_evidence':{'account':{'billing_mode':'USAGE_ONLY'}}}
                    db.execute('INSERT INTO provider_requests VALUES(?,?,?,?,?,?,?,?)',('provider',key,1,json.dumps(body),'instance','retrieval','','key-'+key))
                rows=db.execute(sql,{'scope_id':'provider','account_id':'embedding'}).fetchall()
                self.assertEqual(rows[0][0],'z-unknown' if product else 'a-known')
                db.execute("DELETE FROM provider_requests WHERE object_id='z-unknown'")
                self.assertEqual(len(db.execute(sql,{'scope_id':'provider','account_id':'embedding'}).fetchall()),0 if product else 1)

    def test_committed_legacy_assemblies_keep_their_exact_original_identities(self):
        settings=resolve_deployment({})
        self.assertEqual(ManagedBootstrap(settings,communication_format=False).assembly_digest,
            '6c23faee15efaa705935c8d8c81e5b0372cb9e97a1a6d3829e72334bbb322d1a')
        self.assertEqual(ManagedBootstrap(settings,product_format=False).assembly_digest,
            '150175a80c69db28111273cd71ac93923e92ffc65ca282bb78949d3ffd4ad3e1')
