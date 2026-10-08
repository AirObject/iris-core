"""R13 keeps numeric and negation differences on both similarity paths."""
from unittest.mock import Mock

import pytest

from iris.retrieval import Retrieval
from test_retrieval import entry, put


@pytest.mark.parametrize('with_vector', [False, True])
@pytest.mark.parametrize('first,second', [
    ('小林每周三前往河边工作室参加木雕课程', '小林每周五前往河边工作室参加木雕课程'),
    ('小林已经连续练习木雕十二年，每周去工作室学习', '小林已经连续练习木雕十三年，每周去工作室学习'),
    ('小林已经连续练习木雕12年，每周去工作室学习', '小林已经连续练习木雕13年，每周去工作室学习'),
    ('小林不喜欢在闷热的教室里长时间练习木雕', '小林喜欢在闷热的教室里长时间练习木雕'),
    ('小林未同意每周都去工作室，认为没有必要', '小林没有同意每周都去工作室，认为未有必要'),
])
def test_r13_number_and_negation_guard_precedes_text_and_vector_similarity(store, with_vector, first, second):
    retrieval = Retrieval(store)
    retrieval.index = Mock() if with_vector else None
    if with_vector:
        retrieval.index.similarity.return_value = 1.0
    common = dict(speaker_subject_id='self', stance='亲历', world='real', event_time=None, about=[], revision=1)
    assert not retrieval._duplicate(dict(common, id=1, content=first), dict(common, id=2, content=second))
    if with_vector:
        retrieval.index.similarity.assert_not_called()


def test_prepare_returns_both_written_weekdays_and_still_removes_same_claim(store):
    entry(store)
    first = put(store, '我每周三前往河边工作室参加木雕课程')
    second = put(store, '我每周五前往河边工作室参加木雕课程')
    duplicate = put(store, '我每周三前往河边工作室参加木雕课程。')
    result = Retrieval(store).prepare('A', text='木雕课程', participants=[], judge=False)
    ids = [m['id'] for m in result['memories']]
    assert second in ids and len(set(ids) & {first, duplicate}) == 1 and len(ids) == 2
    assert all(m['reason'] == 'relevant' for m in result['memories'])
