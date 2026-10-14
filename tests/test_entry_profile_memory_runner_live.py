"""Evaluation exports the real writer/projection, with labels outside prompts."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_entry_profile_memory_eval import corpus_file
from test_entry_profile_memory_live import RuleGateway


def rule_corpus_file(tmp_path):
    corpus,case=corpus_file(tmp_path)
    # This gateway proposes a rule, so supply actual announcement evidence.
    case['messages'][0]['text']='本群长期禁止广告。'
    case['messages'][1]['text']='同意本群长期禁止广告的群规。'
    case['messages'][0]['at']='2026-10-01T12:00:00+08:00'
    case['messages'][1]['at']='2026-10-04T12:00:00+08:00'
    corpus.write_text(json.dumps({'format_version':1,'cases':[case]},ensure_ascii=False))
    return corpus,case


def test_sqlite_export_has_actual_ids_projection_and_no_self_judgment(tmp_path):
    from evals.entry_profile_eval.memory_runner import run_sqlite
    corpus,case=rule_corpus_file(tmp_path)
    root=tmp_path/'sqlite-run'
    manifest=run_sqlite(corpus,root,gateway_factory=lambda _,store:RuleGateway(store))
    assert manifest['metadata']['repository_mode']=='sqlite'
    assert manifest['metadata']['gate_eligible'] is False  # fake execution is not quality evidence
    doc=json.loads((root/'judging-materials/cases/0001.json').read_text())['input']
    assert doc['projection']['memory_ids']==[m['memory_id'] for m in doc['memories']]
    assert doc['memories'] and all(type(m['memory_id']) is int for m in doc['memories'])
    assert doc['must_cover']==case['must_cover']
    for word in ('supported','privacy_safe','meaning_checks','reason','confirmed_evidence'):
        assert '"'+word+'"' not in json.dumps(doc)
    assert len(manifest['metadata']['prompt_sha256'])==2


def test_rate_limit_invalidates_once_and_preserves_material_without_retry(tmp_path,monkeypatch):
    import httpx
    from iris.models import Gateway, ModelConfig
    from evals.entry_profile_eval.memory_runner import run_sqlite
    corpus,_=corpus_file(tmp_path)
    called=[]
    def serve(request):
        called.append(request)
        return httpx.Response(429,json={'error':{'message':'fixture quota','code':'rate_limit_exceeded'}})
    original=Gateway.__init__
    def configured(self,configs,**kwargs):
        original(self,configs,client=httpx.Client(transport=httpx.MockTransport(serve)),**kwargs)
    monkeypatch.setattr(Gateway,'__init__',configured)
    out=tmp_path/'rate-limited'
    manifest=run_sqlite(corpus,out,configs={'chat':ModelConfig('http://fixture.invalid','','fake')})
    assert len(called)==1
    assert manifest['metadata']['invalidated']['reason']=='rate_limited'
    assert not manifest['metadata']['gate_eligible']
    assert (out/'run.json').is_file() and (out/'judging-materials/cases/0001.json').is_file()


def test_case_clock_also_controls_transaction_projection(tmp_path):
    from evals.entry_profile_eval.memory_runner import run_sqlite
    corpus,case=rule_corpus_file(tmp_path)
    case['as_of']='2040-12-15T23:00:00+08:00'
    case['messages'][0]['at']='2040-12-01T12:00:00+08:00'
    case['messages'][1]['at']='2040-12-02T12:00:00+08:00'
    corpus.write_text(json.dumps({'format_version':1,'cases':[case]},ensure_ascii=False))
    root=tmp_path/'future-corpus-clock'
    run_sqlite(corpus,root,gateway_factory=lambda _,store:RuleGateway(store))
    payload=json.loads((root/'judging-materials/cases/0001.json').read_text())['input']
    assert payload['memories']
    assert payload['projection']['memory_ids']==[m['memory_id'] for m in payload['memories']]
    assert payload['projection']['generated_at']=='2040-12-02T03:00:00+08:00'  # First daily admission, not final read.
