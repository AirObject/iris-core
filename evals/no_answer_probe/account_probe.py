"""Read-only capability probes using configured credentials; never serialize credentials."""
from __future__ import annotations
import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from iris.models import load_test_models


def run(out: Path):
    configs = load_test_models()
    out.mkdir(parents=True, exist_ok=True)
    records = []
    safe_id = re.compile(r'^[a-zA-Z0-9_.:/-]{1,160}$')
    def request(purpose, method, suffix, payload=None):
        config = configs[purpose]
        parsed = urlsplit(config.base_url)
        assert parsed.scheme == 'https' and not parsed.query and not parsed.username
        record = {'purpose': purpose, 'host': parsed.hostname, 'base_path': parsed.path,
                  'method': method, 'path': suffix, 'requested_model': (payload or {}).get('model')}
        start = time.perf_counter()
        try:
            with httpx.Client(timeout=45, follow_redirects=False) as client:
                r = client.request(method, config.base_url + suffix,
                    headers={'Authorization': 'Bearer ' + config.api_key}, json=payload)
            record['http_status'] = r.status_code
            try:
                data = r.json()
            except ValueError:
                data = {}
            error = data.get('error', {}) if isinstance(data, dict) else {}
            if isinstance(error, dict):
                code = str(error.get('code', ''))
                if safe_id.fullmatch(code):
                    record['error_code'] = code
            if suffix == '/models' and r.status_code == 200:
                items = data.get('data', [])
                record['models'] = [x['id'] for x in items if isinstance(x, dict)
                    and isinstance(x.get('id'), str) and safe_id.fullmatch(x['id'])]
                record['model_count'] = len(items)
            if suffix == '/chat/completions' and r.status_code == 200:
                msg = data['choices'][0]['message']
                record['body_nonempty'] = bool(msg.get('content'))
                record['finish_reason'] = data['choices'][0].get('finish_reason')
                record['usage'] = data.get('usage', {})
                record['reasoning_effort'] = payload.get('reasoning_effort')
                record['reasoning_present'] = bool(msg.get('reasoning_content'))
                record['reasoning_chars'] = len(msg.get('reasoning_content') or '')
        except Exception as exc:
            record['exception_type'] = type(exc).__name__
        record['duration_ms'] = (time.perf_counter()-start)*1000
        # Defense in depth: only allowlisted fields above, then redact secret values.
        encoded = json.dumps(record, ensure_ascii=False)
        for c in configs.values():
            if c.api_key:
                encoded = encoded.replace(c.api_key, '[REDACTED]')
        record = json.loads(encoded)
        records.append(record)
        (out/'account-probe.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(record, ensure_ascii=False), flush=True)
    seen = set()
    for purpose in ('chat', 'embedding'):
        c = configs[purpose]
        if c.base_url in seen:
            continue
        seen.add(c.base_url)
        request(purpose, 'GET', '/models')
    for model in ('doubao-seed-rerank', 'base-multilingual-rerank', 'm3-v2-rerank'):
        request('chat', 'POST', '/rerank', {'model': model, 'query': '几点关门？',
            'documents': ['商店每天十八点关门。'], 'top_n': 1})
    for effort in ('low','high'):
        request('chat','POST','/chat/completions', {'model': configs['chat'].model,
            'reasoning_effort':effort, 'max_tokens':2048, 'temperature':0,
            'messages':[{'role':'user','content':'判断记忆能否回答问题，只输出 JSON {"answerable": true或false}。问题：几点关门？记忆：商店每天十八点关门。'}]})
    print(json.dumps({'configured_models':{p:{'model':c.model,'dimensions':c.dimensions,'reasoning_effort':c.reasoning_effort} for p,c in configs.items()}}, ensure_ascii=False))

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    run(p.parse_args().out)
