"""Fresh interpreter confirms original native receipts without new sends."""
import asyncio
import json
from pathlib import Path
import sys
from types import MappingProxyType
from companion_memory.persistence import Found,Committed,RecoveryHandle,OperationIdentity
from companion_memory.persistence.semantic_records import number
from tests.semantic.test_semantic_host import make_host,opened


async def main():
    root,port,work,kind,generation,serialized=sys.argv[1:]
    data=json.loads(serialized)
    handle=RecoveryHandle(OperationIdentity(**data['identity']),data['command_version'],data['fingerprint_version'],data['fingerprint'])
    host=make_host(Path(root),int(port))
    try:
        assert type(await opened(host,'OPEN_EXISTING')) is Found
        provider=host.embedding;management=host.semantic
        assert provider is not None and management is not None
        definition=provider.definitions[kind] if kind=='store_embedding_handoff' else management.definitions[kind]
        resolved=await host.storage.bind_operation(definition,handle.identity.scope_id).resolve_operation(handle)
        assert type(resolved) is Committed,resolved
        assert resolved.receipt.fingerprint==handle.fingerprint and resolved.receipt.identity==handle.identity
        if kind!='bind':
            if kind=='publish_generation':
                publication=await management.owner.memory.rows.read('semantic_publication',management.owner.memory.root_id)
                assert publication is not None
                result=await management.publish(generation,number(publication['material_seq']))
            else:result=await management.run_work(work)
            assert type(result) is MappingProxyType,result
            assert result['state']==('PUBLISHED' if kind=='publish_generation' else 'APPLIED'),result
        assert provider.executions==0
        print(json.dumps({'kind':kind,'sends':0,'original_fingerprint':handle.fingerprint}),flush=True)
    finally:assert await host.close()


if __name__=='__main__':asyncio.run(main())
