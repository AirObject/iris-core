"""Bounded safe diagnostic window with scope-projected opaque cursor capabilities.

Raw events and files are never read here. The unified encoder passes its one
owned record. A random server-side token conceals global positions and binds the
reader, filter and fixed page waterline; token storage is bounded by the window.
"""
from __future__ import annotations
import asyncio
from collections import OrderedDict,deque
from dataclasses import dataclass
from datetime import datetime,timezone
import json
import secrets
from threading import RLock
import time
from types import MappingProxyType
from typing import cast

from companion_memory.configuration import ConfigurationCandidate,runtime_snapshot_issue
from companion_memory.persistence.schema import valid_identifier
from ._encoding import _EncodedEvent
from ._events import _Record
from ._rules import _LEVEL_NUMBERS,_MODULES,_MESSAGES


@dataclass(frozen=True,slots=True)
class ObservationGrant:
    """Trusted scope and separate global metadata permission; no write authority."""
    instance_id:str
    entry_ids:tuple[str,...]
    instance_diagnostics:bool
    window_metadata:bool
    diagnostics_observe:bool=True


@dataclass(frozen=True,slots=True)
class LogReadFailed:
    code:str
    operation:str
    field:str
    reason:str
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class LogPage:
    value:MappingProxyType[str,object]


@dataclass(frozen=True,slots=True)
class LogGap:
    value:MappingProxyType[str,object]


@dataclass(slots=True)
class _Cursor:
    reader:RuntimeLogReader
    query:dict[str,object]
    after:int
    waterline:int
    expires:float
    lost:int=0


class RuntimeLogReader:
    """Native scoped query capability; no full window, file or audit access."""
    __slots__=('_window',)
    def __new__(cls):raise TypeError('Bind a reader through trusted observation assembly.')
    def __setattr__(self,name,value):raise AttributeError('Read capabilities are immutable.')
    async def query_runtime_logs(self,query:object):
        window=_reader_window(self)
        return await window.query(self,query) if window else LogReadFailed('ACCESS_DENIED','query_runtime_logs','capability','GRANT_INVALID')
    def read_window_health(self):
        window=_reader_window(self)
        return window.health(self) if window else LogReadFailed('ACCESS_DENIED','query_runtime_logs','capability','GRANT_INVALID')


def _reader_window(reader:object):
    if type(reader) is not RuntimeLogReader:return None
    try:window=object.__getattribute__(reader,'_window')
    except AttributeError:return None
    return window if type(window) is RuntimeLogWindow else None


class RuntimeLogWindow:
    """One current-process bounded window with independent output/query lifecycle."""
    def __init__(self,candidate:ConfigurationCandidate,instance_id:str):
        if runtime_snapshot_issue(candidate) is not None or not valid_identifier(instance_id):
            raise ValueError('A complete runtime configuration and instance binding are required.')
        settings=candidate.runtime
        self._capacity=settings.integer('logging.web_window_events')
        self._row_limit=settings.integer('logging.web_query_row_limit')
        self._byte_limit=settings.integer('logging.web_query_max_bytes')
        self._timeout=settings.integer('logging.web_query_timeout_ms')/1000
        self._query_capacity=settings.integer('management.observation_concurrency')
        self._ttl=settings.integer('management.refresh_min_interval_ms')/1000*self._capacity
        self._instance_id=instance_id
        self._lock=RLock();self._events:deque[tuple[int,_EncodedEvent]]=deque()
        self._readers:dict[RuntimeLogReader,ObservationGrant]={}
        self._cursors:OrderedDict[str,_Cursor]=OrderedDict()
        self._jobs:dict[asyncio.Task,RuntimeLogReader]={}
        self._sequence=0;self._evicted=0;self._closed=False;self._failed=False;self._saturated=False
        self._generation=secrets.token_hex(16)

    def bind_runtime_log_reader(self,grant:ObservationGrant) -> RuntimeLogReader:
        """Issue only explicit instance-matching scope; a metadata bit adds no events."""
        if type(grant) is not ObservationGrant or not valid_identifier(grant.instance_id) or grant.instance_id!=self._instance_id or type(grant.diagnostics_observe) is not bool or not grant.diagnostics_observe or type(grant.entry_ids) is not tuple or any(not valid_identifier(i) for i in grant.entry_ids) or any(type(b) is not bool for b in (grant.instance_diagnostics,grant.window_metadata,grant.diagnostics_observe)):
            raise ValueError('Observation authorization is invalid.')
        reader=object.__new__(RuntimeLogReader);object.__setattr__(reader,'_window',self)
        with self._lock:self._readers[reader]=grant
        return reader

    def revoke(self,reader:RuntimeLogReader) -> None:
        """Trusted authorization change invalidates the capability and all its cursors."""
        with self._lock:
            self._readers.pop(reader,None)
            for key in tuple(self._cursors):
                if self._cursors[key].reader is reader:del self._cursors[key]

    @staticmethod
    def _visible(record:_Record,grant:ObservationGrant) -> bool:
        context=dict(record.context)
        if 'entry_id' in context:return context['entry_id'] in grant.entry_ids
        return grant.instance_diagnostics

    def offer_encoded(self,event:_EncodedEvent) -> None:
        """Internal post-normalization output; allocation failure never affects sinks."""
        with self._lock:
            if self._closed:return
            if self._sequence>=2**63-1:self._saturated=True;self._failed=True;return
            self._sequence+=1
            if len(self._events)==self._capacity:
                sequence,old=self._events.popleft()
                self._evicted=min(2**63-1,self._evicted+1)
                for cursor in self._cursors.values():
                    grant=self._readers.get(cursor.reader)
                    if grant and sequence>cursor.after and self._visible(old.record,grant) and self._matches(old.record,cursor.query):cursor.lost+=1
            self._events.append((self._sequence,event))

    def _failure(self,code:str,field:str,reason:str,pending:bool=False) -> LogReadFailed:
        return LogReadFailed(code,'query_runtime_logs',field,reason,pending)

    def _validate(self,reader:object,query:object) -> dict[str,object] | LogReadFailed:
        grant=self._readers.get(cast(RuntimeLogReader,reader)) if type(reader) is RuntimeLogReader else None
        if grant is None:return self._failure('ACCESS_DENIED','capability','GRANT_INVALID')
        if type(query) is not dict or any(type(k) is not str for k in query):return self._failure('INVALID_QUERY','query','INVALID_SHAPE')
        allowed={'cursor','limit','start','end','minimum_level','modules','event_codes','entry_id','run_id','request_id','attempt_id'}
        if set(query)!=allowed:return self._failure('INVALID_QUERY','query','INVALID_SHAPE')
        if query['entry_id'] is not None and (type(query['entry_id']) is not str or query['entry_id'] not in grant.entry_ids):return self._failure('ACCESS_DENIED','capability','SCOPE_DENIED')
        if type(query['limit']) is not int or not 1<=query['limit']<=self._row_limit:return self._failure('INVALID_QUERY','query','LIMIT_EXCEEDED')
        if query['cursor'] is not None and (type(query['cursor']) is not str or len(query['cursor'])!=43):return self._failure('INVALID_QUERY','query','CURSOR_MISMATCH')
        for key in ('entry_id','run_id','request_id','attempt_id'):
            if query[key] is not None and not valid_identifier(query[key]):return self._failure('INVALID_QUERY','query','INVALID_SHAPE')
        for key,choices in (('modules',_MODULES),('event_codes',_MESSAGES)):
            values=query[key]
            if (type(values) is not tuple and type(values) is not list) or len(values)>16 or any(type(v) is not str or v not in choices for v in values):return self._failure('INVALID_QUERY','query','FILTER_UNSUPPORTED')
        if query['minimum_level'] is not None and (type(query['minimum_level']) is not str or query['minimum_level'] not in _LEVEL_NUMBERS):return self._failure('INVALID_QUERY','query','FILTER_UNSUPPORTED')
        for key in ('start','end'):
            value=query[key]
            if value is not None:
                if type(value) is not str or len(value)>64:return self._failure('INVALID_QUERY','query','INVALID_SHAPE')
                try:
                    parsed=datetime.fromisoformat(value)
                    if parsed.utcoffset() is None:return self._failure('INVALID_QUERY','query','INVALID_SHAPE')
                except ValueError:return self._failure('INVALID_QUERY','query','INVALID_SHAPE')
        if query['start'] is not None and query['end'] is not None and datetime.fromisoformat(query['start'])>=datetime.fromisoformat(query['end']):return self._failure('INVALID_QUERY','query','INVALID_SHAPE')
        return {k:tuple(v) if type(v) is list else v for k,v in query.items()}

    @staticmethod
    def _matches(record:_Record,query:dict[str,object]) -> bool:
        context=dict(record.context)
        if any(query[k] is not None and context.get(k)!=query[k] for k in ('entry_id','run_id','request_id','attempt_id')):return False
        if query['modules'] and record.logger not in cast(tuple[str,...],query['modules']):return False
        if query['event_codes'] and record.event_code not in cast(tuple[str,...],query['event_codes']):return False
        if query['minimum_level'] is not None and record.level_number<_LEVEL_NUMBERS[cast(str,query['minimum_level'])]:return False
        timestamp=datetime.fromisoformat(record.timestamp)
        return not ((query['start'] is not None and timestamp<datetime.fromisoformat(cast(str,query['start']))) or (query['end'] is not None and timestamp>=datetime.fromisoformat(cast(str,query['end']))))

    def _token(self,cursor:_Cursor) -> str:
        token=secrets.token_urlsafe(32)
        self._cursors[token]=cursor
        while len(self._cursors)>self._capacity:self._cursors.popitem(last=False)
        return token

    def _page(self,reader:RuntimeLogReader,query:dict[str,object]):
        with self._lock:
            checked=self._validate(reader,query)
            if type(checked) is LogReadFailed:return checked
            grant=self._readers[reader]
            now=time.monotonic();observed=datetime.now(timezone.utc).isoformat()
            after=0;waterline=self._sequence
            token=cast(str | None,query['cursor'])
            filters={k:v for k,v in query.items() if k not in ('cursor','limit')}
            if token is not None:
                cursor=self._cursors.get(token)
                if cursor is not None and (cursor.reader is not reader or cursor.query!=filters):return self._failure('ACCESS_DENIED','capability','SCOPE_DENIED')
                if cursor is None or cursor.expires<now or cursor.lost:
                    restart=self._token(_Cursor(reader,filters,0,self._sequence,now+self._ttl))
                    result:dict[str,object]={'reason':'CONTINUITY_UNCONFIRMED','observed_at':observed,'coverage':'CURRENT_PROCESS_WINDOW','restart_cursor':restart,'lost_authorized_events':'UNKNOWN'}
                    if grant.window_metadata:result['global_gap']={'reason':'CURSOR_EXPIRED' if cursor is None or cursor.expires<now else 'WINDOW_EVICTED','generation':self._generation,'available_range':(self._events[0][0] if self._events else 0,self._sequence),'lost_count':'UNKNOWN'}
                    return LogGap(cast(MappingProxyType[str,object],_freeze(result)))
                after=cursor.after;waterline=self._sequence if cursor.after>=cursor.waterline else cursor.waterline
            snapshot=tuple(self._events)
            oldest=snapshot[0][0] if snapshot else 0
        matches=[(seq,item) for seq,item in snapshot if after<seq<=waterline and self._visible(item.record,grant) and self._matches(item.record,filters)]
        selected=matches[:cast(int,query['limit'])];more=len(matches)>len(selected)
        next_after=selected[-1][0] if more else waterline
        events=tuple(json.loads(item.jsonl) for _,item in selected)
        with self._lock:
            if reader not in self._readers:return self._failure('ACCESS_DENIED','capability','GRANT_INVALID')
            current_oldest=self._events[0][0] if self._events else self._sequence+1
            lost=sum(1 for seq,_ in matches if next_after<seq<current_oldest)
            next_token=self._token(_Cursor(reader,filters,next_after,waterline,now+self._ttl,lost))
        result={'events':events,'next_cursor':next_token,'has_more':more,'observed_at':observed,'coverage':'CURRENT_PROCESS_WINDOW'}
        if grant.window_metadata:result['window_metadata']={'generation':self._generation,'as_of_sequence':waterline,'oldest_available_sequence':oldest}
        if len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())>self._byte_limit:return self._failure('INVALID_QUERY','query','LIMIT_EXCEEDED')
        return LogPage(cast(MappingProxyType[str,object],_freeze(result)))

    async def query(self,reader:RuntimeLogReader,query:object):
        with self._lock:
            checked=self._validate(reader,query)
            if type(checked) is LogReadFailed:return checked
            if self._closed or self._failed:return self._failure('UNAVAILABLE','state','WINDOW_CLOSED' if self._closed else 'RESOURCE_FAILURE')
            if len(self._jobs)>=self._query_capacity:return self._failure('RESOURCE_BUSY','state','QUERY_CAPACITY')
            assert type(checked) is dict
            task=asyncio.create_task(asyncio.to_thread(self._page,reader,checked))
            self._jobs[task]=reader
            task.add_done_callback(self._finished)
        done,_=await asyncio.wait((task,),timeout=self._timeout)
        return task.result() if done else self._failure('TIMEOUT','state','QUERY_DEADLINE',True)

    def _finished(self,task:asyncio.Task) -> None:
        if not task.cancelled():task.exception()
        with self._lock:self._jobs.pop(task,None)

    def health(self,reader:RuntimeLogReader):
        with self._lock:
            grant=self._readers.get(reader)
            if grant is None:return self._failure('ACCESS_DENIED','capability','GRANT_INVALID')
            result:dict[str,object]={'observed_at':datetime.now(timezone.utc).isoformat(),'availability':'UNAVAILABLE' if self._closed or self._failed else 'AVAILABLE','query_pending':any(r is reader for r in self._jobs.values()),'coverage':'CURRENT_PROCESS_WINDOW'}
            if grant.window_metadata:result['window_metadata']={'generation':self._generation,'oldest_sequence':self._events[0][0] if self._events else 0,'latest_sequence':self._sequence,'retained_events':len(self._events),'capacity':self._capacity,'evicted_events':self._evicted,'query_occupancy':len(self._jobs),'saturated':self._saturated}
            return _freeze(result)

    def mark_unavailable(self) -> None:
        """Record output failure independently of console/file and durable audit."""
        with self._lock:self._failed=True

    def close(self) -> bool:
        """Close new queries; outstanding workers retain their references until done."""
        with self._lock:self._closed=True;return not self._jobs


def _freeze(value):
    if type(value) is dict:return MappingProxyType({k:_freeze(v) for k,v in value.items()})
    if type(value) is tuple or type(value) is list:return tuple(_freeze(v) for v in value)
    return value
