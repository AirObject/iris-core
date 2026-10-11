"""Handwritten mechanism fixtures, never quality evaluation samples."""
import copy
import json

import pytest

from iris.db import dumps
from iris.entry_profile import EntryProfileEngine, current_profile, admin_edit, rollback
from iris.models import ModelReply
from test_entry_profile import Clock, Model, messages, memory, TEXTS


@pytest.fixture
def clock():
    return Clock()


def item(kind='saying', label='翻面', quotes=None, **extra):
    return {'kind':kind,'label':label,'quotes':quotes or [{'message_id':1,'text':'反看试印纸背面'}],**extra}


def run(store,clock,items,check_hook=None,expected=0):
    fake=Model(store)
    original=fake.chat
    def chat(messages,purpose,**kwargs):
        reply=original(messages,purpose,**kwargs)
        if purpose.endswith('generate'):
            return ModelReply(dumps({'items':items}),'stop',{})
        value=json.loads(reply.content)
        if check_hook:
            check_hook(value)
        return ModelReply(dumps(value),'stop',{})
    fake.chat=chat
    return EntryProfileEngine(store,fake,clock=clock).regenerate('group',expected_version=expected),fake


def setup(store,clock):
    messages(store,clock,text='反看试印纸背面可以检查渗墨')


def test_render_structured_saying_and_unsupported_meaning_removed(store,clock):
    setup(store,clock)
    def checker(value):
        value['sentences'][0]['meaning_supported']=False
    result,fake=run(store,clock,[item(meaning='看看空气中的湿度')],checker)
    profile=result['version']
    assert result['status']=='published'
    assert profile['content']=='群内说法：翻面（“反看试印纸背面”）。\n'
    assert profile['sentences'][0]['meaning']==''
    assert profile['checks']['removed_meanings']
    assert profile['checks']['format_version']==2
    assert fake.calls[1][1]['candidate'][0]['meaning']=='看看空气中的湿度'
    assert profile['checks']['quote_validation']['passed']==1


@pytest.mark.parametrize('quote,reason',[
    ({'message_id':1,'text':'反看背面'},'quote_not_found'),
    ({'message_id':999999,'text':'反看试印纸背面'},'invalid_quote_reference'),
    ({'message_id':True,'text':'反看试印纸背面'},'invalid_quote_reference'),
    ({'message_id':1,'text':''},'invalid_quote'),
])
def test_invalid_quote_rejects_item(store,clock,quote,reason):
    setup(store,clock)
    result,fake=run(store,clock,[item(quotes=[quote])])
    assert result['status']=='rejected'
    assert result['version']['checks']['deleted_sentences'][0]['reason']==reason
    assert not current_profile(store,'group') and len(fake.calls)==1


def test_quote_whitespace_only_normalization_and_quoted_original(store,clock):
    setup(store,clock)
    with store.write() as conn:
        conn.execute("UPDATE messages SET quote_content='翻面\t 是\n 反看纸背面',quote_author_subject_id='self' WHERE id=1")
    result,_=run(store,clock,[item(quotes=[{'message_id':1,'text':'翻面 是 反看纸背面'}])])
    quote=result['version']['sentences'][0]['quotes'][0]
    assert result['status']=='published' and quote['field']=='quote'
    assert quote['speaker']=='self'


def test_foreign_message_and_truncation_marker_are_not_evidence(store,clock):
    setup(store,clock)
    foreign=messages(store,clock,1,entry='other',text='别群原话')[0]
    result,_=run(store,clock,[item(quotes=[{'message_id':foreign,'text':'别群原话'}])])
    assert result['status']=='rejected'
    clock.advance(days=1)
    messages(store,clock,1,text='很长的原话'*200)
    result,_=run(store,clock,[item(quotes=[{'message_id':52,'text':'已截断'}])])
    assert result['status']=='rejected'


@pytest.mark.parametrize('kind', ['topic','atmosphere','habit'])
def test_single_message_topics_and_old_summary_kinds_are_rejected(store,clock,kind):
    setup(store,clock)
    result,_=run(store,clock,[item(kind=kind)])
    assert result['status']=='rejected'


def test_topic_requires_different_days_and_independent_originals(store,clock):
    setup(store,clock)
    quotes=[{'message_id':1,'text':'反看试印纸背面'},{'message_id':2,'text':'检查渗墨'}]
    result,_=run(store,clock,[item(kind='topic',label='试印渗墨',quotes=quotes)])
    assert result['status']=='rejected'
    clock.advance(days=1)
    new=messages(store,clock,1,text='这张试印纸背面有渗墨')[0]
    quotes[1]={'message_id':new,'text':'试印纸背面有渗墨'}
    result,_=run(store,clock,[item(kind='topic',label='试印渗墨',quotes=quotes)])
    assert result['status']=='published'
    clock.advance(days=1)
    with store.write() as conn:
        conn.execute("UPDATE messages SET content='引用上次的话',quote_content=(SELECT content FROM messages WHERE id=1),quote_author_subject_id='self' WHERE id=?",(new,))
    quotes[1]={'message_id':new,'text':'检查渗墨'}
    result,_=run(store,clock,[item(kind='topic',label='试印渗墨',quotes=quotes)],expected=2)
    assert result['status']=='rejected'
    assert current_profile(store,'group')['version']==2


@pytest.mark.parametrize('text',['QQ:12345678','微信号:fake_account','<@123456789>', 'user_id=demo_42'])
def test_account_identifiers_filtered_even_with_matching_quotes(store,clock,text):
    messages(store,clock,text=text)
    result,_=run(store,clock,[item(quotes=[{'message_id':1,'text':text}])])
    assert result['status']=='rejected'
    assert result['version']['checks']['deleted_sentences'][0]['reason']=='account_identifier'


def test_memory_source_quotes_keep_revision_basis_and_visible_evidence(store,clock):
    setup(store,clock)
    old=messages(store,clock,1,days_ago=20,text='这句原话说明翻面的含义')[0]
    mid=memory(store,clock,[old],text='翻面指反看纸背面')
    result,fake=run(store,clock,[item(meaning='反看纸背面',memory_refs=['M1'],
        quotes=[{'message_id':old,'text':'原话说明翻面的含义'}])])
    assert result['status']=='published'
    from iris.admin_data import entry_profile_version
    detail=entry_profile_version(store,'group',1)
    assert detail['sentences'][0]['basis'][0]['memory_id']==mid
    assert detail['sentences'][0]['basis'][0]['evidence']['source_messages'][0]['message_id']==old
    assert fake.calls[1][1]['candidate'][0]['evidence']['memories'][0]['memory_id']==mid


def test_deleted_count_is_model_only_and_combines_both_stages(store,clock):
    setup(store,clock)
    admin=admin_edit(store,'group','管理员说明'+('字'*150)+'。',expected_version=0,clock=clock)
    def checker(value):
        value['sentences'][0]['supported']=False
    result,_=run(store,clock,[item(),item(label='坏引用',quotes=[{'message_id':999,'text':'不存在'}])],checker,expected=1)
    assert result['status']=='rejected'
    assert result['version']['checks']['original_item_count']==2
    assert len(result['version']['checks']['deleted_sentences'])==2
    assert current_profile(store,'group')['version']==admin['version']


def test_budget_and_item_limit_reject_without_dropping_admin(store,clock):
    setup(store,clock)
    result,_=run(store,clock,[item(label=f'词{i}') for i in range(7)])
    assert result['status']=='rejected' and 'too_many_items' in result['version']['checks']['errors']
    clock.advance(days=1)
    admin=admin_edit(store,'group','字'*499+'。',expected_version=0,clock=clock)
    result,_=run(store,clock,[item()],expected=admin['version'])
    assert result['status']=='rejected'
    assert result['version']['sentences'][-1]['text']==admin['content']


def test_v2_edit_preserves_whole_items_with_quote_punctuation_and_short_rollback(store,clock):
    setup(store,clock)
    result,_=run(store,clock,[item()])
    first=result['version']
    text=first['content']+'管理员说明。'*23
    edited=admin_edit(store,'group',text,expected_version=1,clock=clock)
    assert edited['sentences'][0]==first['sentences'][0]
    restored=rollback(store,'group',1,expected_version=2,clock=clock)
    assert restored['content']==first['content']


def test_old_v1_remains_readable_and_next_update_replaces_only_model_sentences(store,clock):
    setup(store,clock)
    legacy=admin_edit(store,'group',''.join(TEXTS),expected_version=0,clock=clock)
    with store.write() as conn:
        sentences=copy.deepcopy(legacy['sentences'])
        for sentence in sentences:
            sentence['author']='model'
        conn.execute('UPDATE entry_profile_versions SET sentences_json=? WHERE version=1',(dumps(sentences),))
    assert current_profile(store,'group')['content']==legacy['content']
    result,_=run(store,clock,[item()],expected=1)
    assert result['status']=='published' and '群内说法' in result['version']['content']
    assert result['version']['content']!=legacy['content']


@pytest.mark.parametrize('bad',[{'kind':[],'label':'词','quotes':[]},None,{'kind':'saying','label':'词','quotes':True}])
def test_malformed_items_fail_closed_without_unexpected_error(store,clock,bad):
    setup(store,clock)
    result,_=run(store,clock,[bad])
    assert result['status']=='rejected' and result['reason']!='unexpected_error'


def test_publication_only_changes_prepare_profile_partition(store,clock):
    from iris.retrieval import Retrieval
    from test_retrieval import put
    setup(store,clock)
    put(store,'试印纸张背面有渗墨')
    retrieval=Retrieval(store,clock=clock)
    before=retrieval.prepare('group',text='试印纸张',participants=[],judge=False)
    search_before=retrieval.search(text='试印纸张')
    result,_=run(store,clock,[item()])
    assert result['status']=='published'
    after=retrieval.prepare('group',text='试印纸张',participants=[],judge=False)
    search_after=retrieval.search(text='试印纸张')
    assert after['entry_profile']['text']==result['version']['content']
    assert before['entry_profile'] is None
    for response in (before,after):
        response.pop('entry_profile');response.pop('recall_id')
        response['judgment'].pop('duration_ms')
        for hint in response['hints']:
            if hint.get('code')=='recall_judgment':
                hint.pop('duration_ms')
    for response in (search_before,search_after):
        response.pop('recall_id')
    assert before==after and search_before==search_after


def test_quoted_copy_cannot_supply_a_second_topic_day(store,clock):
    setup(store,clock)
    later=messages(store,clock,1,days_ago=1,text='这是一次转引')[0]
    with store.write() as conn:
        conn.execute("UPDATE messages SET quote_content='反看试印纸背面可以检查渗墨0',quote_author_subject_id='self' WHERE id=?",(later,))
    quotes=[{'message_id':later,'text':'反看试印纸背面'},{'message_id':1,'text':'反看试印纸背面'},
            {'message_id':2,'text':'检查渗墨'}]
    result,_=run(store,clock,[item(kind='topic',label='试印渗墨',quotes=quotes)])
    assert result['status']=='rejected'


def test_manual_item_echo_is_preserved_once_and_excluded_from_deletion_denominator(store,clock):
    setup(store,clock)
    first,_=run(store,clock,[item()])
    original=first['version']['sentences'][0]
    # Model a previously handwritten item while preserving a real structured line.
    with store.write() as conn:
        conn.execute('UPDATE entry_profile_versions SET sentences_json=? WHERE version=1',
                     (dumps([{**original,'author':'admin','basis':[]}]),))
    clock.advance(days=1)
    result,_=run(store,clock,[item()],expected=1)
    assert result['status']=='published'
    assert len(result['version']['sentences'])==1
    assert result['version']['sentences'][0]['author']=='admin'
    assert result['version']['checks']['original_item_count']==0


def test_longer_memory_excerpt_is_available_when_daily_sampling_shortens_body(store,clock):
    from iris.entry_profile import _evidence_index, _resolve, select_material
    setup(store,clock)
    memory(store,clock,[1])
    material=select_material(store,'group',clock=clock)
    # Same persisted message and hash; smaller deterministic daily excerpt.
    material['messages'][0]['content']='反看'
    candidate=item(quotes=[{'message_id':1,'text':'检查渗墨'}])
    resolved,deleted,errors,_,_= _resolve({'items':[candidate]},material,None)
    assert not deleted and not errors
    assert resolved[0]['basis'][0]['memory_id']
    assert _evidence_index(material)[0][1]['content'].endswith('渗墨0')
