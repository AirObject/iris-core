"""Two-message native DeepSeek rehearsal with a fresh process at every recovery.

The HTTP server is literal loopback and all credentials and decisions are
synthetic. It emits one valid memory independent of the human scoring standard,
then empty results. The first learning response crosses the old five-second
boundary. All original wire, database and result artifacts remain available.
"""
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from typing import cast
from .test_deepseek import envelope
from tests.cognition.test_text_output import proposal
from .files import write_new, canonical, digest
from .materials import corpus, BODIES


def qualify(root: Path, platform: str) -> dict:
    root.mkdir(mode=0o700);data=root/'data';data.mkdir(mode=0o700)
    requests=[];failures=[];responses=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            wire=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(wire)
            user=json.loads(wire['messages'][1]['content'])
            if 'initial_input' in user:
                output={'schema_version':1,'text':'Synthetic external preset with no lived experience.','initial_input_ids':[user['initial_input']['object_id']]}
            elif len(requests)==2:
                time.sleep(5.3)
                target=next(v for v in user['members'] if v['member']['role']=='TARGET')
                item=proposal();item.update(body='Synthetic qualification statement.',subject_ids=['person-cen'],speaker_subject_id=None,
                    target_anchors=[{'message_id':target['member']['message_id'],'part':'BODY','item_index':None,'start_utf8':None,'end_utf8':None}],basis_refs=[],auxiliary_refs=[])
                output={'schema_version':1,'memories':[item]}
            else:output={'schema_version':1,'memories':[]}
            response=envelope(json.dumps(output,ensure_ascii=False));response['id']='synthetic-response-'+str(len(requests));responses.append(response)
            body=canonical(response);self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,format: str,*args: object) -> None:
            """Keep synthetic request logs in the bounded structured artifacts."""
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=lambda:server.serve_forever(.01));thread.start()
    commands=[]
    def step(action: str,ordinal: int=0):
        output=root/(action+'-'+str(ordinal)+'.json')
        argv=[sys.executable,'-m','tests.provider_trials.deepseek_synthetic_worker',action,'--root',str(data),'--output',str(output),
            '--platform',platform,'--ordinal',str(ordinal)]
        if action in ('persona','learn'):argv+=['--port',str(server.server_port)]
        started=time.time_ns();process=subprocess.run(argv,capture_output=True,text=True,timeout=90)
        record={'argv':argv,'exit_code':process.returncode,'stdout':process.stdout,'stderr':process.stderr,'started_ns':started,'ended_ns':time.time_ns()}
        commands.append(record);write_new(root/(action+'-'+str(ordinal)+'-command.json'),canonical(record))
        assert process.returncode==0,record
        return json.loads(output.read_text())
    try:
        persona=step('persona');recovered=step('recover-persona')
        assert len(requests)==1 and persona['persona']==recovered['persona'] and persona['budget']==recovered['budget']
        elapsed=None
        for ordinal in range(6):
            actual=step('learn',ordinal);count=len(requests);restored=step('recover',ordinal)
            for field in ('receipt','work','provider','objects','sources','persona','budget'):
                assert actual[field]==restored[field],field
            assert len(requests)==count and actual['receipt']['terminal']=='SUCCEEDED'
            assert len(actual['objects'])==(1 if ordinal==0 else 0)
            if ordinal==0:
                elapsed=actual['learning_elapsed_seconds'];assert elapsed>5.3 and elapsed<30
                assert actual['sources'] and actual['queries']
            batch=cast(list[dict[str,list[int]]],corpus()['batches'])[ordinal]
            sent=json.loads(requests[-1]['messages'][1]['content'])['members']
            for role,key in (('HISTORY','history'),('TARGET','target'),('RECENT','recent')):
                assert [v['event']['body'] for v in sent if v['member']['role']==role]==[BODIES[i] for i in batch[key]]
        assert len(requests)==7
        with sqlite3.connect(data/'database/runtime.sqlite3') as db:
            rows=[json.loads(row[0]) for row in db.execute('SELECT body FROM provider_attempts')]
            assert len(rows)==7 and all(r['usage']['format_version']==4 and r['usage']['cost_complete'] and r['state']=='COMPLETED' for r in rows)
            assert all(r['first_error'] is None and r['terminal_error'] is None for r in rows)
        result={'platform':platform,'supplier_attempts':0,'synthetic_attempts':7,'recoveries':7,'recovery_additional_sends':0,
            'formal_objects':1,'full_source_and_public_query':True,'first_learning_elapsed_seconds':elapsed,
            'all_windows_frozen':True,'all_cleanups_ended':True,'uid':os.getuid(),'python':sys.version,'sqlite':sqlite3.sqlite_version,
            'openssl':ssl.OPENSSL_VERSION,'ca_certificates':len(ssl.create_default_context().get_ca_certs()),
            'wire_max_bytes':max(len(canonical(w)) for w in requests),'wire_digests':[digest(w) for w in requests]}
        write_new(root/'qualification.json',canonical(result));return result
    finally:
        server.shutdown();thread.join(5);server.server_close()
        write_new(root/'requests.json',canonical(requests));write_new(root/'responses.json',canonical(responses))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--platform',choices=('macos','linux'),required=True)
    args=parser.parse_args();print(json.dumps(qualify(args.root.resolve(),args.platform)))

if __name__=='__main__':main()
