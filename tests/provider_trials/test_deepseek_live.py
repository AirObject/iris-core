"""Activated launch guards and the native persona worker using only loopback.

The same launch implementation seeds, sends once and recovers original results.
Synthetic credentials and approval live solely in private temporary directories;
no ordinary test loads user preparation or contacts the supplier.
"""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.configuration.text_codec import candidate_values
from companion_memory.configuration.content_codec import entries_digest
from companion_memory.persistence.text_records import stable_identity
from companion_memory.provider.credentials import CredentialResolver,Available,CredentialLease
from tests.provider.test_chat_transport import server
from . import live_worker
from .deepseek_configuration import configuration
from .deepseek_live import admission
from .authorization import AuthorizationJournal
from .test_deepseek_authorization import allowance
from .test_controls import GOOD
from .test_deepseek import envelope
from .files import canonical,digest,write_new
from .host import assemble


def packet(root: Path):
    with TemporaryDirectory() as temporary:
        config,_=configuration(Path(temporary).resolve());encoded=candidate_values(config)
        encoded=json.loads(json.dumps(encoded).replace(str(Path(temporary).resolve()),str(root)))
        for domain in encoded['domains']:
            domain['digest']=entries_digest(tuple((e['parameter_key'],e['body']) for e in domain['entries']))
    template=allowance();template['independent_trial']['new_roots']={'macos':str(root),'linux':'/synthetic-deepseek/linux'}
    template.update(package_digest=None,code_digest=None,approved_by=None,approval_ref=None)
    for key in ('count_approved','money_approved','independent_under_old_unknown_approved','user_decision_ref'):template['independent_trial'][key]=None
    value={'activation_template':template,'configurations':{'macos':{'native_domains':encoded}}}
    approval=copy.deepcopy(template);approval.update(package_digest=digest(value),code_digest=live_worker.manifest()[1],approved_by='synthetic-human',approval_ref='synthetic-decision')
    approval['independent_trial'].update(count_approved=True,money_approved=True,independent_under_old_unknown_approved=True,user_decision_ref='synthetic-decision')
    return value,approval


class LaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_reservation_precedes_attempt_and_recovery_never_resolves_credential(self):
        with TemporaryDirectory() as temporary:
            outer=Path(temporary).resolve();root=outer/'deepseek';package,approval=packet(root)
            journal=AuthorizationJournal(outer/'journal');journal.create(approval)
            network=patch.object(live_worker,'controlled_network',return_value={'synthetic':True,'probe_http_bytes':0})
            with network:
                admission(package,journal.read_snapshot(),root,'macos','seed',None)
                await live_worker.execute(root,'macos','seed',outer/'seed',None,None,supplier='deepseek',frozen_package=package)
                with self.assertRaises(ValueError):admission(package,journal.read_snapshot(),root,'macos','persona',None)
                token=journal.reserve('macos:persona',digest(package),approval['code_digest'],GOOD)
                permit={'operation':'macos:persona','code_digest':approval['code_digest'],'package_digest':digest(package),'token':token,'root':str(root),'platform':'macos'}
                path=outer/'permit.json';write_new(path,canonical(permit))
                admission(package,journal.read_snapshot(),root,'macos','persona',permit)
                initial=stable_identity('self-input','deepseek-text-trial-macos','deepseek-trial-macos')
                response=envelope(json.dumps({'schema_version':1,'text':'Synthetic external preset.','initial_input_ids':[initial]}));raw=canonical(response)
                frame=f'HTTP/1.1 200 OK\r\nContent-Length: {len(raw)}\r\n\r\n'.encode()+raw
                resolutions=[]
                def resolve(*args):resolutions.append(args);return Available(CredentialLease(b'synthetic-only'))
                resolver=CredentialResolver(resolve)
                with server(frame) as (port,requests,failures):
                    def local_assemble(*args,**kwargs):return assemble(*args,**kwargs,loopback_port=port)
                    with patch.object(live_worker,'assemble',side_effect=local_assemble):
                        sent=await live_worker.execute(root,'macos','persona',outer/'sent',path,outer/'unused',resolver,supplier='deepseek',frozen_package=package,validated_permit=permit)
                    self.assertEqual(len(requests),1);self.assertEqual(len(resolutions),1)
                    self.assertEqual(sent['run']['state'],'WAITING_REVIEW')
                    self.assertIsNone(sent['run']['publication_id']);self.assertTrue(sent['close_report'])
                    restored=await live_worker.execute(root,'macos','recover',outer/'recovered',None,None,supplier='deepseek',frozen_package=package)
                    for key in ('run','pending','provider','budget'):self.assertEqual(sent[key],restored[key])
                    self.assertEqual(len(requests),1);self.assertEqual(len(resolutions),1)
                    self.assertFalse(restored['provider_health_after_close']['cleanup_pending'])
                candidate=sent['pending']['candidate']
                decision={'approved_by':'synthetic-human','approval_ref':'synthetic-explicit-review','package_digest':digest(package),
                    'candidates':{'macos':{'decision':'APPROVE','candidate_id':candidate['object_id'],'revision':1,
                    'candidate_digest':sent['pending']['candidate_digest'],'text_digest':candidate['text_digest'],'run_id':candidate['run_id']}}}
                bad=copy.deepcopy(decision);bad['candidates']['macos']['candidate_digest']='e'*64
                with self.assertRaises(ValueError):
                    await live_worker.execute(root,'macos','publish',outer/'bad-review',None,None,supplier='deepseek',frozen_package=package,user_approval=bad)
                published=await live_worker.execute(root,'macos','publish',outer/'published',None,None,supplier='deepseek',frozen_package=package,user_approval=decision)
                self.assertEqual(published['current_persona']['text'],candidate['text'])
                restored=await live_worker.execute(root,'macos','recover',outer/'publication-recovered',None,None,supplier='deepseek',frozen_package=package)
                for key in ('current_persona','run','provider','budget'):self.assertEqual(published[key],restored[key])
                from http.server import HTTPServer,BaseHTTPRequestHandler
                import threading
                from tests.cognition.test_text_output import proposal
                wires=[]
                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        wire=json.loads(self.rfile.read(int(self.headers['Content-Length'])));wires.append(wire)
                        user=json.loads(wire['messages'][1]['content']);items=[]
                        if len(wires)==1:
                            target=next(m for m in user['members'] if m['member']['role']=='TARGET')
                            item=proposal();item.update(body='Synthetic independent statement.',subject_ids=['person-cen'],speaker_subject_id=None,
                                target_anchors=[{'message_id':target['member']['message_id'],'part':'BODY','item_index':None,'start_utf8':None,'end_utf8':None}],basis_refs=[],auxiliary_refs=[])
                            items=[item]
                        raw=canonical(envelope(json.dumps({'schema_version':1,'memories':items})))
                        self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                    def log_message(self,format: str,*args: object) -> None:pass
                http=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=lambda:http.serve_forever(.01));thread.start()
                try:
                    from companion_memory.retrieval.query_service import QueryPort
                    from companion_memory.information.errors import InformationError,InformationRejected
                    original_query=QueryPort.search_memory
                    busy_requests=[]
                    async def busy_once(port,request):
                        busy_requests.append(request)
                        if len(busy_requests)==1:return InformationRejected(InformationError('RESOURCE_BUSY','search_memory','state','ADMISSION_FULL'))
                        return await original_query(port,request)
                    first_query_recovery={}
                    for ordinal in range(6):
                        learning_permit={**permit,'operation':'macos:learn-'+str(ordinal)}
                        def learning_assemble(*args,**kwargs):return assemble(*args,**kwargs,loopback_port=http.server_port)
                        with patch.object(live_worker,'assemble',side_effect=learning_assemble),patch.object(QueryPort,'search_memory',busy_once):
                            learned=await live_worker.execute(root,'macos','learn',outer/('learn-'+str(ordinal)),path,outer/'unused',resolver,
                                supplier='deepseek',frozen_package=package,validated_permit=learning_permit,ordinal=ordinal)
                        self.assertEqual(learned['receipt']['terminal'],'SUCCEEDED')
                        self.assertEqual(len(learned['objects']),1 if ordinal==0 else 0)
                        recovered=await live_worker.execute(root,'macos','recover-learning',outer/('recover-'+str(ordinal)),None,None,
                            supplier='deepseek',frozen_package=package,ordinal=ordinal)
                        for key in ('receipt','work','provider','budget','objects','sources','current_persona'):self.assertEqual(learned[key],recovered[key])
                        if ordinal==0:first_query_recovery=recovered
                        self.assertEqual(len(wires),ordinal+1)
                        self.assertEqual(recovered['budget'][0]['attempt_count'],ordinal+2)
                    self.assertEqual(len(resolutions),7)
                    self.assertIs(busy_requests[0],busy_requests[1])
                    async def deny(port,request):return InformationRejected(InformationError('ACCESS_DENIED','search_memory','capability','BINDING_MISMATCH'))
                    with patch.object(QueryPort,'search_memory',deny),self.assertRaises(ValueError):
                        await live_worker.execute(root,'macos','recover-learning',outer/'query-failure',None,None,supplier='deepseek',frozen_package=package,ordinal=0)
                    failed=json.loads((outer/'query-failure/result.json').read_text())
                    self.assertEqual(failed['provider']['attempts'][0]['state'],'COMPLETED')
                    self.assertTrue(failed['provider']['attempts'][0]['usage']['cost_complete'])
                    self.assertEqual(failed['budget'][0]['attempt_count'],7)
                    self.assertEqual(len(resolutions),7)
                    query_recovered=first_query_recovery
                    # Reuse the original full query snapshot. A later replay of
                    # an already delivered query can legitimately be CONFIRMED_ONLY.
                    # Reproduce the old collection ordering in synthetic data:
                    # committed result, a busy local query, absent accounting.
                    partial=copy.deepcopy(failed);partial.pop('provider');partial.pop('budget')
                    partial['queries'][0]['result']="InformationRejected RESOURCE_BUSY ADMISSION_FULL"
                    for variant in ('unknown','cost','cleanup','valid'):
                        recovered_proof=copy.deepcopy(query_recovered)
                        if variant=='unknown':recovered_proof['provider']['attempts'][0]['ever_unknown']=True
                        if variant=='cost':recovered_proof['provider']['attempts'][0]['usage']['cost_complete']=False
                        if variant=='cleanup':recovered_proof['provider_health_after_close']['cleanup_pending']=True
                        record={'original_result_digest':digest(partial),'recovery_result_digest':digest(recovered_proof)}
                        corrected=AuthorizationJournal(outer/('correction-'+variant));corrected.create(approval)
                        reservation=corrected.reserve('macos:learn-0',digest(package),approval['code_digest'],GOOD)
                        corrected.settle(reservation,evidence_digest=digest(record),attempts=1,money=approval['per_attempt_money_bound'],quota=0,
                            remote_known=False,local_committed=True,cleanup_ended=True,cost_complete=False,integrity_ok=False)
                        if variant!='valid':
                            with self.assertRaises(ValueError):corrected.correct_local_observation(partial,recovered_proof,record)
                        else:
                            original_stop=copy.deepcopy(corrected.read_snapshot()['events'][-1])
                            corrected.correct_local_observation(partial,recovered_proof,record)
                            snapshot=corrected.read_snapshot()
                            self.assertEqual(snapshot['events'][-2],original_stop)
                            self.assertEqual(snapshot['events'][-1]['retained_money_responsibility'],approval['per_attempt_money_bound'])
                            import time
                            with patch('tests.provider_trials.authorization.time.time_ns',return_value=time.time_ns()+31_000_000_000):
                                corrected.reserve('macos:learn-1',digest(package),approval['code_digest'],GOOD)
                            with self.assertRaises(ValueError):corrected.correct_local_observation(partial,recovered_proof,record)

                finally:http.shutdown();thread.join(5);http.server_close()


    async def test_activation_rejects_root_material_code_and_retry_changes(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()/'deepseek';package,approval=packet(root);snapshot={'approval':approval,'events':[]}
            for action in ('persona-retry','learn','unknown'):
                with self.assertRaises(ValueError):admission(package,snapshot,root,'macos',action,None)
            for mutate in ('code','root','old_stop','material'):
                changed=copy.deepcopy(snapshot);value=copy.deepcopy(package)
                if mutate=='code':changed['approval']['code_digest']='e'*64
                if mutate=='root':changed['approval']['independent_trial']['new_roots']['macos']='/other-deepseek'
                if mutate=='old_stop':changed['approval']['independent_trial']['old_stop_evidence_digest']='f'*64
                if mutate=='material':value['unexpected']='changed'
                with self.assertRaises(ValueError):admission(value,changed,root,'macos','seed',None)
