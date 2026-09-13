"""Explicit bounded test-driver confirmation of original public operations.

The automatic local index is allowed to compete for real resources. Test setup
confirms its same retained keys when admission or delivery loses that race; it
never hides a model retry in production or treats UNKNOWN as another send.
"""
import asyncio
from collections.abc import Awaitable,Callable
import time
from companion_memory.persistence import Unconfirmed,NotCommitted,Rejected as StorageRejected
from companion_memory.runtime.results import Rejected as RuntimeRejected
from companion_memory.self_model.results import Rejected as PersonaRejected
from companion_memory.configuration.text_persistent_results import ConfigurationUnconfirmed


async def confirm_local(call: Callable[[],Awaitable[object]]) -> object:
    """Use the supplied original-key call; no new key, material or policy is made."""
    deadline=time.monotonic()+12
    value:object=None
    for _ in range(16):
        value=await call()
        retry=type(value) in (Unconfirmed,ConfigurationUnconfirmed)
        if type(value) is NotCommitted:
            retry=value.error is None or value.error.reason in ('ADMISSION_BUSY','PARTICIPANT_REJECTED','LOCK_DEADLINE')
        if type(value) is StorageRejected or type(value) is RuntimeRejected or type(value) is PersonaRejected:
            retry=value.error.code=='RESOURCE_BUSY' or value.error.reason=='REVISION_CONFLICT'
        if not retry or time.monotonic()>=deadline:return value
        await asyncio.sleep(.05)
    return value


async def complete_learning(entry,key: str) -> object:
    """Drive up to three explicit confirmations after observed trusted NOT_SENT.

    Each new trigger follows an actual WAITING_ADMISSION observation. This is
    test-operator policy, distinct from the original-key confirmation helper.
    Actual server request counts must still prove one successful send per batch.
    """
    from companion_memory.persistence import Found
    from companion_memory.memory.formats import record
    selected=key;value:object=None
    for ordinal in range(3):
        value=await confirm_local(lambda:entry.run_learning(selected))
        if type(value) is not Found or record(value.value).get('state')!='WAITING_ADMISSION':return value
        selected=key+':explicit-confirmation:'+str(ordinal+1)
    return value
