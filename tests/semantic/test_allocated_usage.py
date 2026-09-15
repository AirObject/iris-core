"""Allocated usage, native ledger isolation and real SQLite controlled execution.

Synthetic loopback responses prove engineering only. Actual dispatch intervals
and resource admission remain enabled; no supplier credentials are used.
"""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
import unittest
from companion_memory.configuration.semantic_codec import candidate_values
from companion_memory.configuration.semantic_resolution import resolve_semantic_configuration,SemanticConfigurationOk
from companion_memory.persistence._codec import assembly_value,command_descriptor
from companion_memory.persistence.schema import encode_value,InvalidValue
from companion_memory.persistence import Found,Receipt
from companion_memory.persistence.semantic_records import identity,number
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider.embedding_allocated_usage import observe,validate
from companion_memory.provider.embedding_observation import observer
from companion_memory.provider.embedding_protocol import parse_response
from companion_memory.runtime.semantic_assembly import SemanticAssembly
from tests.semantic.configuration_support import candidate,inputs
from tests.semantic.public_support import establish,activate
from tests.semantic.fault_support import server,lexical
from tests.semantic.test_semantic_host import make_host,opened
from tests.semantic.test_retained_provider_errors import RetainedProviderErrorTests


class AllocatedUsageTests(unittest.TestCase):
    def test_usage_observation_and_strict_vector_identity_are_independent(self):
        cases=((None,'MISSING','UNAVAILABLE'),({},'MISSING','UNAVAILABLE'),
            ({'prompt_tokens':7},'MISSING','PARTIAL'),({'total_tokens':7},'MISSING','PARTIAL'),
            ({'prompt_tokens':7,'total_tokens':7},'OK','COMPLETE'),
            ({'prompt_tokens':True,'total_tokens':7},'INVALID','UNAVAILABLE'),
            ({'prompt_tokens':8,'total_tokens':7},'INVALID','UNAVAILABLE'),
            ({'prompt_tokens':2**63,'total_tokens':7},'INVALID','UNAVAILABLE'),
            ({'prompt_tokens':7,'total_tokens':7,'other':10},'UNSUPPORTED_FIELDS','PARTIAL'))
        for raw,reason,coverage in cases:
            with self.subTest(raw=raw):
                result=observe(json.dumps({'usage':raw}).encode())
                self.assertEqual((result['observation_reason'],result['coverage']),(reason,coverage))
                self.assertIsNone(result['known_cost_atoms']);self.assertIsNone(result['quota_known'])
                self.assertFalse(result['cost_complete']);self.assertEqual(result['held_atoms'],0)
                with self.assertRaises(InvalidValue):validate(dict(result)|{'known_cost_atoms':0})
        value={'id':'request','created':1,'object':'list','model':'doubao-embedding-vision-251215',
            'data':[{'object':'embedding','index':0,'embedding':[1.0]*1024}],'usage':None}
        def parse():return parse_response(json.dumps(value).encode(),space_id='space',
            expected_models=('doubao-embedding-vision','doubao-embedding-vision-251215'),usage_only=True)
        self.assertIsNotNone(parse().result)
        del value['usage'];self.assertIsNone(parse().result)
        value['usage']=None;value['model']='foreign-model';self.assertIsNone(parse().result)

    def test_closed_configuration_static_capacity_and_old_fingerprint(self):
        with TemporaryDirectory() as directory:
            old=SemanticAssembly();old_bytes=assembly_value(old.repositories,old.commands,assembly_format='ASYNC_SEMANTIC_V1')
            new=SemanticAssembly(usage_only=True);new_bytes=assembly_value(new.repositories,new.commands,assembly_format='ASYNC_SEMANTIC_V1')
            self.assertNotEqual(old_bytes,new_bytes);self.assertLessEqual(len(new_bytes),4194304)
            self.assertEqual(len(new.semantic.commands),26);self.assertEqual(new.ledger.repository.definition.schema_version,4)
            configuration,_=candidate(Path(directory),offline=False,usage_only=True)
            encoded=candidate_values(configuration)
            self.assertEqual(sum(len(d['entries']) for d in encoded['domains']),124)
            sizes=[len(encode_value(command_descriptor(c),2097152)) for c in new.commands]
            self.assertLessEqual(max(sizes),2097152)
            for mutation in ('money','profile','transport','duplicate_models'):
                supplied=inputs(Path(directory),offline=False,usage_only=True)
                if mutation=='money':supplied[0]['explicit_values']['provider.accounts'][0]['cost_limit_atoms']=0
                elif mutation=='profile':supplied[0]['explicit_values']['provider.profiles'][0]['max_input_units']=8192
                elif mutation=='transport':supplied[5]['explicit_values']['provider.embedding_transport']['v']=1
                else:supplied[5]['explicit_values']['provider.embedding_transport']['expected_reported_models']=['doubao-embedding-vision']*2
                self.assertIsNot(type(resolve_semantic_configuration(*supplied)),SemanticConfigurationOk)
            print(json.dumps({'static_bytes':len(new_bytes),'maximum_command_descriptor_bytes':max(sizes),
                'old_static_sha256':sha256(old_bytes).hexdigest(),'new_static_sha256':sha256(new_bytes).hexdigest()}))


class AllocatedPublicTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_usage_allows_next_original_slot_after_actual_cleanup(self):
        with server(usage=lambda:{'prompt_tokens':7},model='doubao-embedding-vision-251215') as (port,calls),TemporaryDirectory() as directory:
            root=Path(directory).resolve();host,oid=await establish(root,port,usage_only=True)
            try:
                grant=activate(host);management=host.semantic;provider=host.embedding
                assert management is not None and provider is not None
                self.assertIs(type(await management.resume('resume')),Receipt)
                work=await management.prepare_document(oid,'allocated-document','partition');assert type(work) is str
                first=await management.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid))
                assert type(first) is MappingProxyType,first
                self.assertEqual(first['state'],'APPLIED');self.assertFalse(first['cleanup_pending']);self.assertEqual(len(calls),1)
                native=await provider.ledger.read('requests_page',{'after':'','limit':8});self.assertEqual(len(native),1)
                attempt=(await provider.ledger.read('attempts_for_request',{'request_id':native[0]['object_id']}))[0]
                metering=attempt['usage'];assert type(metering) is MappingProxyType
                self.assertEqual(metering['coverage'],'PARTIAL');self.assertIsNone(metering['known_cost_atoms'])
                self.assertEqual(native[0]['format_version'],4)
                view=await observer(provider).get_budget_state();assert type(view.value) is tuple
                budget_view=view.value[0];assert type(budget_view) is MappingProxyType
                self.assertIsNone(budget_view['available_atoms'])
                control=await management.control();earliest=number(control['last_cleanup_at'])+30000000
                while time.time_ns()//1000<earliest:await asyncio.sleep(min(.5,(earliest-time.time_ns()//1000)/1000000))
                publication=await management.owner.memory.rows.read('semantic_publication',management.owner.memory.root_id);assert publication is not None
                published=await management.publish(identity('semantic-generation','allocated-http',1),number(publication['material_seq']))
                assert type(published) is MappingProxyType and published['state']=='PUBLISHED',published
                await lexical(host)
                from companion_memory.information.management import HostIdentity
                business=await host.bind_business(HostIdentity('allocated-http','principal','host','entry',frozenset(('search_memory',)),(),time.monotonic()+300))
                assert host.queries is not None and host.queries.semantic is not None
                partition=host.queries.semantic.partition(business._query)
                query_work=await management.prepare_query('query 0','query:0',partition);assert type(query_work) is str
                second=await management.run_work(query_work,slot_id=identity('semantic-slot','fixture-package','QUERY','query:0'))
                assert type(second) is MappingProxyType,second
                self.assertEqual(second['state'],'APPLIED');self.assertFalse(second['cleanup_pending']);self.assertEqual(len(calls),2)
                from tests.provider_trials.semantic_live import http_result
                from tests.information.test_queries import query
                payload=query('http-cache',query_text='query 0',category='EVENT',retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False,require_complete=True,allow_partial=False)
                response=await http_result(root,host,business,{'queries':[{'query':payload}]*6},'shared-principal',True)
                self.assertEqual(response['actual_mode'],'HYBRID');self.assertEqual(response['decision'],'NO_MATCH')
                self.assertEqual(response['query_vector']['state'],'CACHE_HIT');self.assertEqual(len(calls),2)
                self.assertTrue(await host.close())
                host=make_host(root,port,usage_only=True);self.assertIs(type(await opened(host,'OPEN_EXISTING')),Found)
                assert host.semantic is not None and host.embedding is not None
                host.bind_activation(grant)
                self.assertEqual(await host.semantic.run_work(work),first)
                self.assertEqual(await host.semantic.run_work(query_work),second)
                self.assertEqual(host.embedding.executions,0);self.assertEqual(len(calls),2)
            finally:self.assertTrue(await host.close())

    async def test_zero_money_unknown_stops_registration_across_reopen(self):
        with server(unknown=True) as (port,calls),TemporaryDirectory() as directory:
            root=Path(directory).resolve();host,oid=await establish(root,port,usage_only=True)
            try:
                activate(host);management=host.semantic;provider=host.embedding
                assert management is not None and provider is not None
                await management.resume('resume')
                work=await management.prepare_document(oid,'allocated-unknown','partition');assert type(work) is str
                ended=await management.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid))
                assert type(ended) is MappingProxyType,ended
                self.assertEqual(ended['state'],'REMOTE_UNKNOWN');self.assertFalse(ended['cleanup_pending'])
                for reopen in (False,True):
                    if reopen:
                        self.assertTrue(await host.close());host=make_host(root,port,usage_only=True)
                        self.assertIs(type(await opened(host,'OPEN_EXISTING')),Found)
                    provider=host.embedding;assert provider is not None
                    budget=await provider.ledger.get('budget_windows',identity('embedding-budget','fixture_embedding','fixture_window'))
                    assert budget is not None
                    self.assertEqual(budget['held_atoms'],0);self.assertEqual(budget['attempt_count'],1)
                    with self.assertRaises(OwnerFailure):await provider.check_budget(ended)
                    self.assertEqual(len(calls),1)
            finally:self.assertTrue(await host.close())


class AllocatedRetainedErrors(RetainedProviderErrorTests):
    """Identical SQLite rollback/confirmation faults against v4 native roots."""
    usage_only=True


from tests.semantic.test_material import SemanticMaterialTests

class AllocatedMaterialCapacity(SemanticMaterialTests):
    """Maximum inherited metadata initialized through the new complete graph."""
    usage_only=True


class AllocatedWireEvidenceTests(unittest.TestCase):
    def test_series_keeps_each_original_body_and_partial_usage(self):
        from companion_memory.provider.wire_evidence import WireEvidence
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();evidence=WireEvidence(root,multiple=True)
            for n in range(2):
                body=json.dumps({'input':['original '+str(n)]}).encode()
                response=json.dumps({'usage':{'prompt_tokens':7,'unknown_units':2}}).encode()
                evidence.request(body);evidence.response('RESPONSE',200,response,None)
                child=root/str(n+1).zfill(2)
                self.assertEqual((child/'request-body.json').read_bytes(),body)
                self.assertEqual((child/'response-body.json').read_bytes(),response)
            self.assertEqual(len(tuple(root.iterdir())),2)
            self.assertEqual((root/'01').stat().st_mode&0o777,0o700)
            self.assertEqual((root/'01/request-body.json').stat().st_mode&0o777,0o600)
            with self.assertRaises(FileExistsError):WireEvidence(root,multiple=True).request(b'no retry')
