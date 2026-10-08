"""Runtime-only GLM learning probe; never persist configuration or HTTP headers."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
import uuid
import httpx
from iris import evaluation, models


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--variant', choices=['default', 'low', 'high'], required=True)
    parser.add_argument('--workers', type=int, choices=[1,4], default=4)
    args = parser.parse_args()
    os.umask(0o077)
    out = args.out.resolve()
    if out.exists():
        raise SystemExit('Refusing to reuse an output directory: runtime overrides are not fingerprinted.')
    out.mkdir(parents=True)
    (out/'responses').mkdir()
    configs = models.load_test_models()
    if args.variant != 'default' and hasattr(configs['chat'], 'reasoning_effort'):
        configs['chat'] = replace(configs['chat'], reasoning_effort=args.variant)
    secrets = [v for c in configs.values() for v in (c.api_key,c.base_url,urlsplit(c.base_url).hostname) if v]
    def sanitized(value):
        serialized=json.dumps(value,ensure_ascii=False)
        for secret in secrets:
            serialized=serialized.replace(secret,'[REDACTED]')
        return json.loads(serialized)
    lock=threading.Lock()
    local=threading.local()
    def write(name, value):
        row=sanitized(value)
        with lock:
            with (out/name).open('a',encoding='utf-8') as f:
                f.write(json.dumps(row,ensure_ascii=False)+'\n')
    def utc():
        return datetime.now(timezone.utc).isoformat()
    extra={} if args.variant=='default' else {'reasoning_effort':args.variant}
    metadata={'variant':args.variant,'extra_request_parameters':extra,'workers':args.workers,
              'effective_reasoning_effort':getattr(configs['chat'],'reasoning_effort',None),
              'learning_timeout_seconds':models.LEARNING_TOTAL_TIMEOUT,'max_tokens':16000,'response_format':{'type':'json_object'},
              'judge_mode':'external','started_at':utc(),'corpus_file_sha256':hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
              'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (out/'probe-metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    original_init=models.Gateway.__init__
    original_call=models.Gateway._call
    original_record=models.Gateway._record
    original_close=models.Gateway.close
    original_case=evaluation._run_case

    class RecordingClient:
        def __init__(self,case):
            self.inner=httpx.Client()
            self.case=case
            self.contexts={}
            self.counts={}
        def post(self,url,**kwargs):
            payload=kwargs['json']
            key=json.dumps({k:v for k,v in payload.items() if k!='model'},ensure_ascii=False,sort_keys=True)
            context=dict(self.contexts[key])
            logical=context['logical_call']
            with lock:
                attempt=self.counts.get(logical,0)+1
                self.counts[logical]=attempt
            event={**context,'attempt':attempt,'case':self.case,'started_at':utc()}
            start=time.monotonic()
            response=None
            try:
                response=self.inner.post(url,**kwargs)
                event.update(seconds=round(time.monotonic()-start,6),status_code=response.status_code,
                             retry_after=response.headers.get('Retry-After'))
                try:
                    data=response.json()
                except ValueError:
                    data={}
                    event['non_json_envelope']=True
                event['usage']=data.get('usage')
                event['error']=data.get('error')
                if context['kind']=='chat':
                    choice=(data.get('choices') or [{}])[0]
                    message=choice.get('message') or {}
                    content=message.get('content') or ''
                    reasoning=message.get('reasoning_content')
                    event.update(finish_reason=choice.get('finish_reason'),message_keys=list(message),
                                 reasoning_chars=len(reasoning) if isinstance(reasoning,str) else None,
                                 content_chars=len(content),moderation_hit_type=choice.get('moderation_hit_type'))
                    try:
                        def no_constant(value):
                            raise ValueError(value)
                        event['strict_json_object']=isinstance(json.loads(content,parse_constant=no_constant),dict)
                    except (ValueError,TypeError):
                        event['strict_json_object']=False
                    filename=f'responses/{logical}-{attempt}.json'
                    (out/filename).write_text(json.dumps(sanitized(data),ensure_ascii=False,indent=2),encoding='utf-8')
                    event['response_file']=filename
                return response
            except Exception as error:
                event.update(seconds=round(time.monotonic()-start,6),exception=type(error).__name__)
                raise
            finally:
                write('http-attempts.jsonl',event)
        def close(self):
            self.inner.close()

    def initialize(self, configs, store=None, client=None, sleeper=time.sleep, **kwargs):
        if client is not None:
            raise RuntimeError('Probe expects the standard evaluator client')
        self.probe_case=getattr(local,'case','unknown')
        original_init(self,configs,store,RecordingClient(self.probe_case),sleeper,**kwargs)

    def call(self,kind,purpose,payload,*,batch_id=None,**kwargs):
        if purpose in ('learning_judge','learning_judge_repair'):
            raise RuntimeError('Dialogue-model judging is forbidden in this probe')
        payload=dict(payload)
        if kind=='chat':
            payload.update(extra)
            effort=getattr(self.configs['chat'],'reasoning_effort',None)
            if effort is not None:
                payload['reasoning_effort']=effort
        logical=uuid.uuid4().hex
        self.probe_logical=logical
        key=json.dumps({k:v for k,v in payload.items() if k!='model'},ensure_ascii=False,sort_keys=True)
        self.client.contexts[key]={'logical_call':logical,'kind':kind,'purpose':purpose,'batch_id':batch_id}
        if kind=='chat':
            write('requests.jsonl',{'logical_call':logical,'case':self.probe_case,'purpose':purpose,'batch_id':batch_id,'payload':payload})
        return original_call(self,kind,purpose,payload,batch_id=batch_id,**kwargs)

    def record(self,purpose,model,duration_ms,category,error,usage=None,flags=None,status_code=None,*,finish_reason=None,batch_id=None,**kwargs):
        original_record(self,purpose,model,duration_ms,category,error,usage,flags,status_code,finish_reason=finish_reason,batch_id=batch_id,**kwargs)
        write('gateway-attempts.jsonl',{'logical_call':self.probe_logical,'case':self.probe_case,'purpose':purpose,
              'batch_id':batch_id,'duration_ms':duration_ms,'category':category,'error':error,'usage':usage,
              'status_code':status_code,'finish_reason':finish_reason,'recorded_at':utc()})
        if purpose.startswith('learning'):
            print(f'{self.probe_case} batch={batch_id} {purpose}: {duration_ms/1000:.3f}s {category} HTTP={status_code} finish={finish_reason}',flush=True)

    def close(self):
        original_close(self)
        self.client.close()

    def run_case(configs,case,judge_runs=2,judge_mode='model'):
        local.case=case['id']
        return original_case(configs,case,judge_runs,judge_mode)

    models.Gateway.__init__=initialize
    models.Gateway._call=call
    models.Gateway._record=record
    models.Gateway.close=close
    evaluation._run_case=run_case
    evaluation.ThreadPoolExecutor=lambda **kw: ThreadPoolExecutor(**{**kw,'max_workers':args.workers})
    try:
        manifest,_=evaluation.run_learning_eval(configs,args.root,split='all',corpus=args.corpus,out=out,judge_mode='external')
        metadata.update(completed_at=utc(),manifest=str(manifest),status='completed')
        print('MATERIALS '+str(manifest),flush=True)
    except BaseException as error:
        metadata.update(completed_at=utc(),status='failed',error_type=type(error).__name__)
        raise
    finally:
        (out/'probe-metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':
    main()
