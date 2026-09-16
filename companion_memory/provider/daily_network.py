"""One physical network worker with no queue and honest completion ownership.

A logical timeout does not cancel or release the worker. A later request becomes
eligible only after the original terminal is confirmed, its real I/O has ended,
and the configured shared quiet interval has elapsed. Recovery starts paused.
"""
from __future__ import annotations
import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass
import math
import time
from typing import TypeVar,Any
from companion_memory.persistence.completion import retain_completion
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import valid_identifier

T=TypeVar('T')

class NetworkPermit:
    """Native identity for one already registered Provider request."""
    _issuer:DailyNetwork
    request_id:str
    operation_key:str
    account_id:str
    deadline:float
    _started:bool
    _ended:float|None
    _terminal:float|None
    _consumer:float|None
    _released:bool
    _future:asyncio.Future[Any]|None
    _done:asyncio.Future[None]
    __slots__=('_issuer','request_id','operation_key','account_id','deadline','_started','_ended','_terminal','_consumer','_released','_future','_done')
    def __setattr__(self,name,value):
        if (not name.startswith('_') or name=='_issuer') and hasattr(self,name):raise AttributeError('Native request identity is immutable.')
        object.__setattr__(self,name,value)
    def __new__(cls,*args,**kwargs):
        raise TypeError('Network permits are issued by the Provider network owner.')

@dataclass(frozen=True,slots=True)
class NetworkObservation:
    """Commit confirmation and physical resource lifetime remain separate facts."""
    paused: bool
    closed: bool
    occupied: bool
    io_pending: bool
    terminal_confirmation_pending: bool
    quiet_until: float|None
    consumer_cleanup_pending: bool=False

class DailyNetwork:
    """Single event-loop owner; only its one worker may call a transport."""
    def __init__(self,admitted:Callable[[str],bool],accounts:tuple[str,...],*,monotonic:Callable[[],float]=time.monotonic):
        if type(accounts) is not tuple or not 1<=len(accounts)<=16 or len(set(accounts))!=len(accounts) or any(not valid_identifier(a) for a in accounts):
            raise OwnerFailure('INVALID_INPUT','account','INVALID_SHAPE')
        self._accounts=frozenset(accounts)
        self._admitted=admitted;self._clock=monotonic
        self._pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='provider-network')
        self._active:NetworkPermit|None=None;self._closed=False;self._paused=True
        self._quiet_until:float|None=None;self._blocked:set[str]=set()
        self._closed_consumer_pending=False
        self._loop:asyncio.AbstractEventLoop|None=None

    def _on_loop(self):
        loop=asyncio.get_running_loop()
        if self._loop is None:self._loop=loop
        if loop is not self._loop:raise OwnerFailure('ACCESS_DENIED','resource','BINDING_MISMATCH')
        return loop

    def resume(self) -> None:
        """The host calls only after a new explicit resume has durably committed."""
        self._on_loop()
        if self._closed:raise OwnerFailure('INVALID_STATE','resource','SERVICE_CLOSED')
        self._paused=False

    def restore_previous_process(self) -> None:
        """Conservatively begin a quiet period after original-process ownership ends.

        A new interpreter cannot observe its predecessor's monotonic I/O end.
        It therefore waits a full interval locally; this creates no request and
        does not change any original business deadline or account restriction.
        """
        self._on_loop()
        if self._active is not None or not self._paused or self._closed:raise OwnerFailure('INVALID_STATE','resource','STATE_MISMATCH')
        self._quiet_until=max(self._quiet_until or 0,self._clock()+30)

    def pause(self) -> None:
        """Revoke new and not-yet-started sends without cancelling actual I/O."""
        self._on_loop();self._paused=True

    def block_account(self,account_id:str) -> None:
        """Restore an unresolved account fence before any startup dispatch."""
        self._on_loop()
        if account_id not in self._accounts:raise OwnerFailure('INVALID_INPUT','account','INVALID_SHAPE')
        self._blocked.add(account_id)

    def reserve(self,request_id:str,operation_key:str,account_id:str,deadline:float,*,consumer_required:bool=False) -> NetworkPermit:
        """Admit zero queued requests, before credential lookup or consumer I/O."""
        loop=self._on_loop();now=self._clock()
        if any(not valid_identifier(value) for value in (request_id,operation_key,account_id)) or type(deadline) not in (float,int) or not math.isfinite(deadline) or not 0<deadline-now<=1200:
            raise OwnerFailure('INVALID_INPUT','request','INVALID_SHAPE')
        if account_id not in self._accounts:raise OwnerFailure('ACCESS_DENIED','account','BINDING_MISMATCH')
        if self._closed:raise OwnerFailure('INVALID_STATE','resource','SERVICE_CLOSED')
        if self._paused or not self._admitted(operation_key):raise OwnerFailure('ACCESS_DENIED','request','OPERATION_NOT_GRANTED')
        if account_id in self._blocked:raise OwnerFailure('INVALID_STATE','account','REMOTE_RESULT_UNKNOWN')
        if self._active is not None or self._quiet_until is not None and now<self._quiet_until:
            raise OwnerFailure('RESOURCE_BUSY','resource','ADMISSION_FULL')
        permit=object.__new__(NetworkPermit)
        permit._issuer=self;permit.request_id=request_id;permit.operation_key=operation_key;permit.account_id=account_id;permit.deadline=deadline
        permit._started=False;permit._ended=None;permit._terminal=None;permit._released=False;permit._future=None;permit._done=loop.create_future()
        permit._consumer=None if consumer_required else now
        self._active=permit
        return permit

    def _validate(self,permit:NetworkPermit) -> None:
        self._on_loop()
        if type(permit) is not NetworkPermit or permit._issuer is not self or self._active is not permit or permit._released:
            raise OwnerFailure('ACCESS_DENIED','request','BINDING_MISMATCH')

    def start(self,permit:NetworkPermit,exchange:Callable[[],T]) -> asyncio.Future[T]:
        """Submit exactly once; the worker rechecks permission before its first I/O."""
        self._validate(permit)
        if permit._started:raise OwnerFailure('PRECONDITION_FAILED','request','NO_CHANGE')
        if self._closed or self._paused or self._clock()>=permit.deadline or not self._admitted(permit.operation_key):
            raise OwnerFailure('ACCESS_DENIED','request','OPERATION_NOT_GRANTED')
        permit._started=True
        def invoke():
            if self._closed or self._paused or self._clock()>=permit.deadline or not self._admitted(permit.operation_key):
                raise OwnerFailure('ACCESS_DENIED','request','OPERATION_NOT_GRANTED')
            return exchange()
        actual=asyncio.wrap_future(self._pool.submit(invoke))
        permit._future=actual
        # Only this operation's actual completion is retained; callers waiting on
        # another request cannot accidentally release or inherit its tail.
        retain_completion(actual)
        def ended(future):
            if not future.cancelled():future.exception()
            permit._ended=self._clock()
            if not permit._done.done():permit._done.set_result(None)
            self._release_if_complete(permit)
        actual.add_done_callback(ended)
        # A cancelled logical waiter cannot cancel the real worker future.
        return asyncio.shield(actual)

    def cancel_unregistered(self,permit:NetworkPermit) -> None:
        """Release only when Provider proved no request could still register.

        No transport was started, so no quiet interval is manufactured. The
        Provider joins its actual storage completion before making this claim.
        """
        self._validate(permit)
        if permit._started or permit._terminal is not None:
            raise OwnerFailure('PRECONDITION_FAILED','request','NO_CHANGE')
        permit._ended=self._clock();permit._released=True
        if not permit._done.done():permit._done.set_result(None)
        self._active=None

    def confirm_terminal(self,permit:NetworkPermit) -> None:
        """Called by Provider only after its original terminal receipt is verified."""
        self._validate(permit)
        if permit._terminal is None:permit._terminal=self._clock()
        if not permit._started:
            permit._ended=self._clock()
            if not permit._done.done():permit._done.set_result(None)
        self._release_if_complete(permit)

    def _release_if_complete(self,permit:NetworkPermit) -> None:
        if permit._ended is None or permit._terminal is None or permit._consumer is None:return
        self._quiet_until=max(permit._ended,permit._terminal,permit._consumer)+30.0
        permit._released=True
        if self._active is permit:self._active=None

    def confirm_consumed(self,permit:NetworkPermit) -> None:
        """Provider confirms durable reception and actual consumer cleanup together."""
        self._validate(permit)
        if permit._consumer is None:permit._consumer=self._clock()
        self._release_if_complete(permit)

    async def wait_io(self,permit:NetworkPermit) -> None:
        """Observe the native operation's actual end, even after logical closure."""
        self._on_loop()
        if type(permit) is not NetworkPermit or permit._issuer is not self:
            raise OwnerFailure('ACCESS_DENIED','request','BINDING_MISMATCH')
        await asyncio.shield(permit._done)

    def observation(self) -> NetworkObservation:
        self._on_loop();active=self._active
        return NetworkObservation(self._paused,self._closed,active is not None,
            active is not None and active._started and active._ended is None,
            active is not None and active._terminal is None,self._quiet_until,self._closed_consumer_pending or active is not None and active._consumer is None)

    def detach_closed(self,permit:NetworkPermit) -> None:
        """Release an ended physical slot on shutdown, retaining consumer debt.

        The Provider calls only after all actual byte and result leases end.
        This records no consumer acknowledgement and can never reopen sending.
        """
        self._validate(permit)
        if not self._closed or permit._ended is None or permit._terminal is None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        self._closed_consumer_pending=permit._consumer is None
        permit._released=True;self._active=None

    async def wait_quiet(self,deadline:float) -> None:
        """Wait before request admission, without occupying or queuing a network slot."""
        self._on_loop()
        if self._closed or self._paused:raise OwnerFailure('ACCESS_DENIED','request','OPERATION_NOT_GRANTED')
        if self._active is not None:raise OwnerFailure('RESOURCE_BUSY','resource','ADMISSION_FULL')
        delay=max(0,(self._quiet_until or self._clock())-self._clock())
        remaining=deadline-time.monotonic()
        if remaining<=delay:raise OwnerFailure('TIMEOUT','request','DEADLINE_EXCEEDED')
        if delay:await asyncio.sleep(delay)
        if self._closed or self._paused:raise OwnerFailure('ACCESS_DENIED','request','OPERATION_NOT_GRANTED')
        if time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','request','DEADLINE_EXCEEDED')

    def close(self) -> NetworkObservation:
        """Stop admission; pending actual work and confirmation remain observable."""
        self._on_loop();self._closed=True;self._paused=True
        self._pool.shutdown(wait=False,cancel_futures=False)
        return self.observation()
