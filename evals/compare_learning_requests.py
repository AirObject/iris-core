"""Offline cross-version learning-request comparison on the public corpora.

Run with this branch's Python environment. --baseline is a git archive/worktree,
--candidate is the other checkout, and --out must be outside both repositories.
No model configuration, API, scoring, or hidden corpus is read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CORPORA = ('learning_v1.jsonl', 'learning_v2.jsonl', 'learning_v3.jsonl', 'learning_v4.jsonl')
STAMP = '2026-10-06T00:00:00+00:00'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def worker(source: Path, target: Path, corpora_root: Path):
    if not (source / 'src/iris/evaluation.py').is_file():
        raise ValueError(f'invalid source checkout: {source}')
    sys.path.insert(0, str(source / 'src'))
    import numpy as np
    from iris import evaluation, retrieval
    from iris.models import ModelConfig
    if not Path(evaluation.__file__).resolve().is_relative_to((source / 'src').resolve()):
        raise ValueError(f'import did not use source checkout: {source}')

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls.fromisoformat(STAMP)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    # Freeze generated timestamps and recency, never corpus event timestamps.
    for name, module in list(sys.modules.items()):
        if name.startswith('iris.') and hasattr(module, 'now'):
            module.now = lambda: STAMP
    retrieval.datetime = FrozenDateTime
    records = []
    current_case = None

    class DeterministicGateway:
        def __init__(self, configs, store):
            self.configs, self.store = configs, store
            self.batch_number = 0

        def json_chat(self, messages, purpose, max_tokens=16000, *, batch_id=None):
            assert purpose == 'learning', f'unexpected call: {purpose}'
            self.batch_number += 1
            material = messages[-1]['content']
            related = material.split('相关已有记忆（数据）：\n', 1)[1].split('—— ', 1)[0]
            block = material.split('—— 目标段（只从这里学习） ——\n', 1)[1].split('—— 后续段', 1)[0]
            memories = []
            # Take each non-scene target message, using its request-local
            # evidence number and participant reference, never an expected label.
            for match in re.finditer(r'^#(\d+) \[[^\n]+?\] \[(他人消息|我实际发言|我行动结果)\] (.*?)：数据：(.*?)(?=\n#|\Z)', block, re.M | re.S):
                number, kind, author, content = match.groups()
                participant = re.match(r'P\d+\b', author)
                speaker = participant.group() if participant else '我'
                memories.append({'content': content[:500], 'type': '事实', 'speaker': speaker,
                                 'about': [speaker], 'stance': '亲历', 'evidence': [int(number)],
                                 'importance': 60, 'belief': 75})
            output = {'memories': memories, 'updates': [], 'people': [], 'goals': [], 'questions': []}
            records.append({'case': current_case, 'batch': self.batch_number,
                            'messages': messages, 'purpose': purpose, 'max_tokens': max_tokens,
                            'related_nonempty': bool(re.search(r'^\[M\d+\]', related, re.M)),
                            'output': output})
            return output, json.dumps(output, ensure_ascii=False), None, 'direct'

        def embedding(self, text, purpose='embedding'):
            # Signed feature hashing: stable Unicode 1/2/3-grams plus a shared
            # component to exercise vectors above the learning cosine floor.
            vector = np.zeros(2048, dtype=np.float32)
            cleaned = ''.join(re.findall(r'\w', text.casefold()))
            for size in (1, 2, 3):
                for start in range(len(cleaned) - size + 1):
                    raw = hashlib.sha256(cleaned[start:start+size].encode()).digest()
                    index = 1 + int.from_bytes(raw[:4], 'big') % 2047
                    vector[index] += 1 if raw[4] & 1 else -1
            norm = np.linalg.norm(vector)
            if norm:
                vector *= .6 / norm
            else:
                vector[1] = .6
            vector[0] = .8
            return vector.tolist()

        def close(self):
            pass

    evaluation.Gateway = DeterministicGateway
    configs = {'chat': ModelConfig('offline', '', 'deterministic-public-target-v1'),
               'embedding': ModelConfig('offline', '', 'doubao-embedding-vision')}
    cases = [json.loads(line) for name in CORPORA for line in (corpora_root/'evals'/name).read_text(encoding="utf-8").splitlines() if line.strip()]
    counts = []
    for case in cases:
        current_case = case['id']
        row = evaluation._run_case(configs, case, judge_mode='external')
        batches = row['actual']['batches']
        assert all(b['state'] == 'succeeded' for b in batches), case['id']
        assert all(a['parse_status'] == 'direct' for a in row['actual']['attempts']), case['id']
        assert len(row['actual']['memories']) > 0, case['id']
        counts.append({'case': case['id'], 'batches': len(batches), 'memories': len(row['actual']['memories'])})
    sources = sorted(p for p in (source/'src/iris').rglob('*') if p.suffix in ('.py', '.md', '.sql', '.json'))
    source_sha256 = hashlib.sha256(b''.join(p.name.encode()+p.read_bytes() for p in sources)).hexdigest()
    target.write_text(json.dumps({'source_sha256': source_sha256, 'cases_sha256': digest(cases), 'requests': records, 'cases': counts}, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--corpora-root', type=Path, help='Shared public corpora checkout (default: candidate)')
    args = parser.parse_args()
    if args.worker:
        worker(args.worker.resolve(), args.out, (args.corpora_root or args.worker).resolve())
        return
    baseline, candidate, out = args.baseline.resolve(), args.candidate.resolve(), args.out.resolve()
    assert not any(out.is_relative_to(p) for p in (baseline, candidate)), 'write full requests outside repositories'
    corpora_root = (args.corpora_root or candidate).resolve()
    out.mkdir(parents=True, exist_ok=False)
    reports = []
    for label, source in (('main', baseline), ('candidate', candidate)):
        target = out / (label + '-requests.json')
        subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', str(source), '--corpora-root', str(corpora_root), '--out', str(target)], check=True)
        reports.append(json.loads(target.read_text(encoding="utf-8")))
    old, new = reports
    assert old['cases_sha256'] == new['cases_sha256']
    keys = lambda r: (r['case'], r['batch'])
    before, after = [{keys(r): r for r in x['requests']} for x in reports]
    differences = []
    for key in before.keys() | after.keys():
        a, b = before.get(key), after.get(key)
        if a is None or b is None or any(a[f] != b[f] for f in ('messages', 'purpose', 'max_tokens')):
            differences.append({'case': key[0], 'batch': key[1]})
    result = {'cases': len(old['cases']), 'batches_main': len(before), 'batches_candidate': len(after),
              'nonempty_main': sum(r['related_nonempty'] for r in before.values()),
              'nonempty_candidate': sum(r['related_nonempty'] for r in after.values()),
              'memories_main': sum(c['memories'] for c in old['cases']),
              'memories_candidate': sum(c['memories'] for c in new['cases']),
              'different_batches': len(differences), 'differences': differences,
              'cases_sha256': old['cases_sha256'],
              'corpora': list(CORPORA),
              'source_sha256_main': old['source_sha256'],
              'source_sha256_candidate': new['source_sha256'],
              'request_sha256_main': digest([r['messages'] for r in old['requests']]),
              'request_sha256_candidate': digest([r['messages'] for r in new['requests']]),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'normalization': ['Generated now() timestamps and retrieval clock fixed at '+STAMP,
                                'Trace identity is public case ID plus ordinal batch number.',
                                'No normalization of system/user message content, participant or memory refs, order, whitespace, or corpus timestamps.'],
              'case_counts_main': old['cases'], 'case_counts_candidate': new['cases']}
    (out/'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if not k.startswith('case_counts')}, ensure_ascii=False, indent=2))
    if differences:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
