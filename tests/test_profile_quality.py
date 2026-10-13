"""Prompt-version wiring through real engines with fake models; no quality corpus."""
from importlib.resources import files

from iris.entry_profile import EntryProfileEngine
from test_entry_profile import Clock, Model, messages


def test_entry_profile_uses_v2_generation_and_check_prompts_outside_transactions(store):
    clock = Clock()
    messages(store, clock)
    model = Model(store)
    prompts = []
    original = model.chat

    def chat(request, purpose, **kwargs):
        assert not store._writer.in_transaction
        prompts.append((purpose, request[0]['content']))
        return original(request, purpose, **kwargs)

    model.chat = chat
    result = EntryProfileEngine(store, model, clock=clock).regenerate('group', expected_version=0)
    assert result['status'] == 'published'
    assert [purpose for purpose, _ in prompts] == [
        'consolidation_entry_profile_generate', 'consolidation_entry_profile_check']
    for (_, prompt), kind in zip(prompts, ('generate', 'check'), strict=True):
        assert prompt == files('iris').joinpath(f'prompts/entry_profile_{kind}_v2.md').read_text(encoding='utf-8')


def named_topic(first, second, quotes, *, label='《湖岸画册》的配色'):
    from iris.entry_profile import _resolve
    evidence = {'entry': {'id': 'group'}, 'memories': [], 'messages': [
        {'message_id': 1, 'entry_id': 'group', 'date': '2026-10-01', 'content': first,
         'speaker': '甲', 'sha256': 'first'},
        {'message_id': 2, 'entry_id': 'group', 'date': '2026-10-02', 'content': second,
         'speaker': '乙', 'sha256': 'second'},
        {'message_id': 3, 'entry_id': 'group', 'date': '2026-10-03',
         'content': '《湖岸画册》这套颜色挺有意思。', 'speaker': '丙', 'sha256': 'unbound'},
    ]}
    return _resolve({'items': [{'kind': 'topic', 'label': label, 'quotes': quotes}]}, evidence, None)


def test_entry_v2_restores_main_without_the_withdrawn_named_topic_gate():
    kept, deleted, errors, count, stats = named_topic(
        '《湖岸画册》的这一页用了绿色。', '这本下一页的蓝色也不错。',
        [{'message_id': 1, 'text': '《湖岸画册》的这一页用了绿色。'},
         {'message_id': 2, 'text': '这本下一页的蓝色也不错。'}])
    assert len(kept) == 1 and not deleted and not errors
    assert count == 1 and stats == {'total': 2, 'passed': 2}


def test_entry_document_candidates_are_not_selectable(store):
    import pytest
    assert EntryProfileEngine(store, None).prompt_version == 'v2'
    for version in ('v3', 'v4', 'v5', 'v6', 'v7'):
        with pytest.raises(ValueError):
            EntryProfileEngine(store, None, prompt_version=version)


# Historical document-path regression on the pre-switch schema.
from profile_legacy import legacy_schema
