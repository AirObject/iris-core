"""Fresh full-public-dev check using the selected real total deadline."""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import re
from pathlib import Path
import time
import httpx
import numpy as np
from iris.models import load_test_models
from probe import (ROOT,digest,write,one_call,model_payload,gate_accept,apply_judge,groups,regressions)


def freeze_run_protocol(path, protocol):
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != protocol:
            raise ValueError("live protocol changed; choose a new label")
    else:
        write(path, protocol)


async def main(out, label="live-check", concurrency=2, interval=.25):
    if out.is_relative_to(ROOT):
        raise ValueError("full artifacts must stay outside repository")
    summary=json.loads((out/'summary.json').read_text())
    capture=json.loads((out/'capture.json').read_text())
    chosen=summary['selected'];params=chosen['params'];config=load_test_models()['chat']
    assert re.fullmatch(r'[a-z0-9-]+',label)
    assert concurrency in (1,2) and interval>=0
    assert config.model==capture['configuration']['chat']['model']
    assert config.base_url==capture['configuration']['chat']['base_url']
    base_protocol=json.loads((out/'protocol.json').read_text(encoding='utf-8'))
    prompt_sha=hashlib.sha256((Path(__file__).parent/'judgment_prompt.txt').read_bytes()).hexdigest()
    if prompt_sha != base_protocol['prompt_sha256']:
        raise ValueError('judgment prompt differs from captured protocol')
    protocol={'selected':chosen['id'],'concurrency':concurrency,'interval_seconds':interval,
              'deadline':params['budget'],'label':label,'prompt_sha256':prompt_sha,
              'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'capture_protocol_sha256':capture['protocol_sha256'],
              'parameters':params,'model':config.model,'endpoint':config.base_url,'reselect':False}
    freeze_run_protocol(out/(label+'-protocol.json'),protocol)
    sem=asyncio.Semaphore(concurrency);completed=0;started=time.perf_counter()
    async with httpx.AsyncClient(timeout=params['budget'],follow_redirects=False) as client:
        async def job(row):
            nonlocal completed
            body=model_payload(row,params['k'])
            fp=digest({'input':body,'selected':chosen['id'],'protocol':capture['protocol_sha256'],
                       'model':config.model,'endpoint':config.base_url,'run_protocol':protocol})
            target=out/label/f"{row['corpus']}--{row['id']}.json"
            if target.exists():
                call=json.loads(target.read_text())
                if call['fingerprint']!=fp:raise ValueError('live fingerprint mismatch')
            else:
                if not body['candidates'] or (params['gate'] and not gate_accept(row,params['gate'])):
                    call={'status':'skipped_empty','scores':{},'usage':{},'duration_ms':0}
                else:
                    async with sem:
                        proxy=ResponseMetadata(client)
                        call=await one_call(proxy,config,body,params['effort'],params['budget'])
                        call.update(proxy.metadata)
                        if config.api_key:
                            call=json.loads(json.dumps(call,ensure_ascii=False).replace(config.api_key,'[REDACTED]'))
                        await asyncio.sleep(interval)
                call={'fingerprint':fp,'input':body,**call};write(target,call)
            new,failed=apply_judge(row,call,params['threshold'],params['budget'])
            completed+=1
            if completed%20==0:print(f'live deadline check: {completed}/228; {call["status"]}',flush=True)
            return new,call,failed
        results=await asyncio.gather(*(job(row) for row in capture['rows']))
    rows=[x[0] for x in results];calls=[x[1] for x in results if x[1]['status']!='skipped_empty']
    lat=[c['duration_ms'] for c in calls]
    differences=[]
    replay=json.loads((out/'results'/f"{chosen['id']}.json").read_text())['rows']
    for before,after in zip(replay,rows):
        if before['returned']!=after['returned'] or before['reasons']!=after['reasons']:
            differences.append({'corpus':after['corpus'],'id':after['id'],'replay':before['returned'],
                'live':after['returned'],'replay_reasons':before['reasons'],'live_reasons':after['reasons']})
    record={'selected':chosen['id'],'parameters':params,'run_protocol':protocol,'elapsed_seconds':time.perf_counter()-started,
        'calls':len(calls),'status_counts':dict(Counter(c['status'] for c in calls)),
        'fallbacks':sum(x[2] for x in results),'p50_ms':float(np.percentile(lat,50)),
        'p95_ms':float(np.percentile(lat,95)),
        'usage':{k:sum(c.get('usage',{}).get(k,0) for c in calls)
                  for k in ('prompt_tokens','completion_tokens','reasoning_tokens','cached_tokens')},
        'groups':groups(rows),'regressions':regressions(rows,capture['rows']),
        'differences_from_replay':differences,'rows':rows}
    write(out/(label+'-summary.json'),record)
    print(json.dumps({k:v for k,v in record.items() if k not in ('rows','groups')},ensure_ascii=False),flush=True)
    print(json.dumps(record['groups']['all'],ensure_ascii=False),flush=True)

class ResponseMetadata:
    def __init__(self,client):self.client=client;self.metadata={}
    async def post(self,*args,**kwargs):
        response=await self.client.post(*args,**kwargs)
        if response.status_code>=400:
            try:code=(response.json().get('error') or {}).get('code')
            except (ValueError,AttributeError):code=None
            if isinstance(code,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',code):self.metadata['error_code']=code
            retry=response.headers.get('retry-after')
            if retry and re.fullmatch(r'[0-9.]{1,10}',retry):self.metadata['retry_after_seconds']=retry
        return response


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--label',default='live-check');p.add_argument('--concurrency',type=int,default=2);p.add_argument('--interval',type=float,default=.25)
    args=p.parse_args();asyncio.run(main(args.out.resolve(),args.label,args.concurrency,args.interval))
