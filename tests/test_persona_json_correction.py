"""One grammar correction gets a concrete parser diagnostic, never a new budget."""
import json

import pytest

from iris.db import dumps
from iris.models import ModelError, ModelReply, parse_json_object_with_status
from iris.persona import PersonaEngine
from test_persona import store


class DiagnosticGateway:
    def __init__(self, raw, fixed, *, repeat=False):
        self.raw,self.fixed,self.repeat=raw,fixed,repeat
        self.calls=[];self.now=0.0
    def monotonic(self):
        return self.now
    def chat(self,messages,purpose,**kwargs):
        self.calls.append((messages,purpose,kwargs['_deadline']))
        self.now+=1
        if len(self.calls)==1:
            return ModelReply(self.raw,'stop',{})
        correction=messages[-1]['content']
        assert '只修正 JSON 语法' in correction
        assert '保留所有字段、值和逐句判定' in correction
        assert '不要原样重复' in correction
        assert messages[-2]=={'role':'assistant','content':self.raw}
        try:
            parse_json_object_with_status(self.raw)
        except json.JSONDecodeError as error:
            assert f'第 {error.lineno} 行、第 {error.colno} 列' in correction
            assert error.msg in correction
        except ValueError:
            assert '没有可解析的根 JSON 对象' in correction
        return ModelReply(self.raw if self.repeat else dumps(self.fixed),'stop',{})


@pytest.mark.parametrize('purpose',['persona_generate','persona_check','persona_recheck'])
@pytest.mark.parametrize('raw',[
    '{"sentences":[{"index":1,"supported":false}},{"index":2,"supported":true}]}',
    '{"sentences":[{"index":1 "supported":false}]}',
    'not a JSON object',
])
def test_single_correction_preserves_raw_and_reports_the_parser_location(store,purpose,raw):
    fixed={'sentences':[{'index':1,'supported':False}]}
    if 'index\":2' in raw:
        fixed['sentences'].append({'index':2,'supported':True})
    model=DiagnosticGateway(raw,fixed);outputs={}
    result=PersonaEngine(store,model)._call(purpose,{},outputs)
    assert result==fixed and len(model.calls)==2
    assert [deadline for _,_,deadline in model.calls]==[360,360]
    assert outputs[purpose]==raw and json.loads(outputs[purpose+'_repair'])==fixed
    assert outputs[purpose+'_json_error']['kind'] in ('JSONDecodeError','ValueError')


def test_repeated_invalid_correction_still_fails_after_two_calls(store):
    raw='{"sentences":[{"index":1}},{"index":2}]}'
    model=DiagnosticGateway(raw,{},repeat=True);outputs={}
    with pytest.raises(ModelError) as failure:
        PersonaEngine(store,model)._call('persona_check',{},outputs)
    assert failure.value.category=='invalid_output' and len(model.calls)==2
    assert outputs['persona_check']==outputs['persona_check_repair']==raw
    assert outputs['persona_check_json_error']['kind']=='JSONDecodeError'
    assert outputs['persona_check_repair_json_error']['kind']=='JSONDecodeError'


def test_valid_adverse_json_does_not_request_a_new_opinion(store):
    raw='{"sentences":[{"index":1,"supported":false}],"change_degree":"large","reason":"不支持。"}'
    model=DiagnosticGateway(raw,{});outputs={}
    result=PersonaEngine(store,model)._call('persona_check',{},outputs)
    assert result['sentences'][0]['supported'] is False and len(model.calls)==1
    assert not any('json_error' in key for key in outputs)
