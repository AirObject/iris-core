"""Read-only publication history reconciliation after both owners recover locally.

Retained, audit-validated receipts prove every switch even after physical pages
are reclaimed. The commit-ordered stream uses bounded pages and scalar state;
neither the mutable memory revision nor remaining index pages supplies a count.
"""
from companion_memory.persistence import OperationPort, Found, NotFound
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.records import record, text, integer
from .index import LocalIndex
from .recovery import require


async def verify_publications(index: LocalIndex, operation: OperationPort) -> None:
    """Keep index admission closed until the original switch history agrees."""
    index.ready = False
    sequence = await index.memory.publication_state()
    coordinator = await index._records.read('coordinator', {'instance_id': index.instance_id})
    if coordinator is None: raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
    after = ''; count = 0; cutoff = 0; latest = None; prior_revision = 1
    while True:
        found = await operation.read_receipt_page(after)
        if type(found) is NotFound: break
        if type(found) is not Found:
            raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
        for receipt in found.value:
            result = record(receipt.result); facts = record(result['facts'])
            memory = record(facts['memory']); retrieval = record(facts['retrieval'])
            generation_id = text(retrieval['object_id'])
            generation = await index._records.read('index_generation', {'generation_id': generation_id})
            require(generation is not None)
            if generation is None: raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
            count += 1
            require(memory['object_id'] == index.instance_id and memory['changed_count'] == 1
                and memory['restored_count'] == 0 and memory['from_seq'] == memory['to_seq']
                and cutoff <= integer(memory['to_seq']) <= integer(sequence['last_seq'])
                and memory['previous_revision'] == integer(memory['to_seq']) + count
                and memory['revision'] == integer(memory['previous_revision']) + 1
                and integer(memory['previous_revision']) >= prior_revision
                and retrieval['from_seq'] == memory['from_seq'] and retrieval['to_seq'] == memory['to_seq']
                and retrieval['at_us'] == memory['at_us'] and retrieval['restored_count'] == 0
                and retrieval['revision'] == integer(retrieval['previous_revision']) + 1
                and retrieval['changed_count'] == (3 if count == 1 else 4))
            require(generation['config_snapshot_id'] == index.configuration.snapshot_id
                and generation['status'] in ('ACTIVE', 'RETIRING')
                and integer(generation['revision']) >= integer(retrieval['revision'])
                and integer(generation['captured_seq']) >= integer(memory['to_seq'])
                and integer(generation['contiguous_seq']) >= integer(memory['to_seq'])
                and integer(generation['captured_seq']) <= integer(sequence['last_seq']))
            # A previous switch must be retired exactly once. Its recorded
            # ACTIVE revision precedes the retirement, never a second publish.
            if latest is not None:
                prior = await index._records.read('index_generation', {'generation_id': latest})
                require(prior is not None and prior['status'] == 'RETIRING' and latest != generation_id)
            target_values = result['targets']
            require(type(target_values) is tuple)
            if type(target_values) is not tuple: raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
            targets = tuple(record(value) for value in target_values)
            require(len(targets) == 2 and {text(value['object_id']) for value in targets} == {index.instance_id, generation_id})
            target = next(value for value in targets if value['object_id'] == generation_id)
            require(target['previous_revision'] == retrieval['previous_revision'] and target['revision'] == retrieval['revision'])
            pointer = next(value for value in targets if value['object_id'] == index.instance_id)
            require(pointer['revision'] == integer(pointer['previous_revision']) + 1 and integer(pointer['revision']) <= integer(coordinator['revision']))
            cutoff = integer(memory['to_seq']); latest = generation_id; prior_revision = integer(memory['revision'])
            after = receipt.identity.operation_key
    require((await index._records.rows.read('published_generation_count', {}))[0]['count'] == count)
    require(sequence['revision'] == integer(sequence['last_seq']) + count + 1
        and sequence['published_generation_id'] == latest and sequence['published_seq'] == cutoff
        and coordinator['active_generation'] == latest)
    if latest is not None:
        active = await index._records.read('index_generation', {'generation_id': latest})
        require(active is not None and active['status'] == 'ACTIVE')
    index.ready = True
