"""Memory-owned semantic gaps, application receipts and publication watermarks.

The earliest uncovered sequence survives revision coalescing. Deletion receipts
contain no artifact, and applying one object never covers another object's gap.
"""
from companion_memory.persistence.semantic_records import (ID, N, P, T, H, CONFIG, RECEIPT,
    Record, enum, row, isolate)
from companion_memory.persistence.semantic_records import number
from companion_memory.persistence.schema import InvalidValue

GAP = row(space_id=ID, object_id=ID, object_revision=P, first_uncovered_seq=P, latest_change_seq=P,
          action=enum('UPSERT', 'DELETE'))
ACK = row(space_id=ID, object_id=ID, object_revision=P, resolved_from_seq=P, applied_seq=P,
          action=enum('UPSERT', 'DELETE'), material_digest=(H,), artifact_id=(ID,), evidence_receipt=RECEIPT)
PUBLICATION = row(space_id=ID, config=CONFIG, material_seq=N, published_seq=N,
                  generation_id=(ID,), published_at=(T,))
SCHEMAS = {'semantic_gap': GAP, 'semantic_ack': ACK, 'semantic_publication': PUBLICATION}


def validate(name: str, value: object) -> Record:
    result = isolate(SCHEMAS[name], value)
    if name == 'semantic_gap':
        if result['revision'] != result['object_revision'] or number(result['first_uncovered_seq']) > number(result['latest_change_seq']): raise InvalidValue()
    elif name == 'semantic_ack':
        if number(result['resolved_from_seq']) > number(result['applied_seq']): raise InvalidValue()
        missing = result['artifact_id'] is None or result['material_digest'] is None
        if (result['action'] == 'UPSERT' and missing or result['action'] == 'DELETE' and
                (result['artifact_id'] is not None or result['material_digest'] is not None)): raise InvalidValue()
    elif number(result['published_seq']) > number(result['material_seq']) or (result['generation_id'] is None) != (result['published_at'] is None): raise InvalidValue()
    return result
