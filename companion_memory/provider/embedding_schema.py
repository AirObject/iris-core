"""Provider-owned immutable embedding handoff leaves and retained root metadata.

Receipt confirmation and physical leaf retirement are separate facts. These
schemas do not alter the version or recovery meaning of existing requests.
"""
from companion_memory.persistence.semantic_records import (ID,N,H,RECEIPT,Record,enum,integer,row,record,isolate,leaf_bytes)
from companion_memory.persistence.schema import BoundedTextSchema

HANDOFF_LEAF = row(handoff_id=ID, request_id=ID, attempt_id=ID, ordinal=integer(0,9),
    byte_count=integer(1,4096), digest=H, data_base64=BoundedTextSchema(5464))
PAYLOAD = record(format_version=integer(1,1), byte_count=integer(1,40960), leaf_count=integer(1,10), payload_digest=H)
CLEANUP = record(received_receipt=(RECEIPT,), retired_through=(N,), state=enum('HELD','RELEASABLE','RETIRED'))


def validate_leaf(value: object) -> Record:
    result = isolate(HANDOFF_LEAF, value)
    leaf_bytes(result, maximum=4096)
    return result
