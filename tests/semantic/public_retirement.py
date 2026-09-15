"""Public maintenance assertions using paid artifacts and actual held mappings."""
from __future__ import annotations
from types import MappingProxyType
from typing import TYPE_CHECKING
from unittest import TestCase
from companion_memory.persistence import Found
from companion_memory.persistence.semantic_records import string,number,identity
from companion_memory.memory.formats import record,sequence
from companion_memory.retrieval.semantic_files import VectorFiles
if TYPE_CHECKING:
    from companion_memory.runtime.semantic_host import SemanticHost


async def verify(test:TestCase,host:SemanticHost,partition:str,expected_requests:int) -> None:
    management=host.semantic;provider=host.embedding;files=host.files
    assert management is not None and provider is not None and files is not None
    observer=host.bind_observation(frozenset(('retrieval/semantic','provider/usage','provider/budget')))
    view=await observer.read('retrieval/semantic',{});test.assertIs(type(view),Found,view);assert type(view) is Found
    test.assertFalse(record(view.value)['cleanup_pending']);test.assertLessEqual(len(sequence(record(view.value)['items'])),16)
    query={'start':'2020-01-01T00:00:00+00:00','end':'2040-01-01T00:00:00+00:00','caller_scope':'instance',
        'capability':'EMBEDDING','task_role':None,'profile_id':None,'account_id':None,'group_by':'NONE'}
    usage=await observer.read('provider/usage',query);test.assertIs(type(usage),Found,usage);assert type(usage) is Found
    test.assertEqual(record(usage.value)['sample_count'],expected_requests)
    aggregate=record(sequence(record(usage.value)['rows'])[0]);test.assertEqual(aggregate['held_atoms'],0)
    budgets=await observer.read('provider/budget',{});test.assertIs(type(budgets),Found,budgets)
    before=provider.executions
    cached_before=await host.combination.cache.rows.page('query_embedding_cache') if host.combination.cache else ()
    reused=await management.prepare_query('query 0','paid-query-reuse',partition);assert type(reused) is str,reused
    result=await management.run_work(reused);assert type(result) is MappingProxyType,result
    test.assertEqual(result['state'],'APPLIED');test.assertFalse(result['cleanup_pending']);test.assertIsNone(result['request_ref'])
    cached_after=await host.combination.cache.rows.page('query_embedding_cache') if host.combination.cache else ()
    test.assertEqual(cached_before,cached_after)
    control=await management.control();first=string(control['current_generation'])
    sealed=files.verify(management.owner.space,first,host.checkpoint)
    second=identity('semantic-generation','fixture',2)
    publication=await management.owner.memory.rows.read('semantic_publication',management.owner.memory.root_id);assert publication is not None
    with files.reader(sealed):
        published=await management.publish(second,number(publication['material_seq']));assert type(published) is MappingProxyType,published
        test.assertEqual(published['state'],'PUBLISHED')
        retired=await management.retire(first);assert type(retired) is bool,retired
        test.assertFalse(retired)
    for _ in range(70):
        retired=await management.retire(first);assert type(retired) is bool,retired
        if retired:break
    else:test.fail('Small original generation did not retire in bounded pages.')
    test.assertFalse((files._root/VectorFiles._name(first)).exists())
    original=files.verify(management.owner.space,second,host.checkpoint)
    target=files._root/VectorFiles._name(second)
    # Destruction is confined to this fixture's derived file, never paid records.
    target.unlink()
    rebuilt=await management.rebuild(second)
    from companion_memory.retrieval.semantic_files import SealedGeneration
    assert type(rebuilt) is SealedGeneration,rebuilt
    test.assertEqual(rebuilt.file_digest,original.file_digest)
    test.assertEqual(rebuilt.file_bytes,original.file_bytes)
    test.assertEqual(provider.executions,before)
