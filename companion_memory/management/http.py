"""Real loopback read-only HTTP for explicitly issued scoped test sessions.

This adapter has only observation capabilities and bounded settings. It owns no
storage, configuration writer, model work port or audit reader. Session secrets
are injected by trusted test assembly and never embedded in the page or logs.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import json
import secrets
import time
from types import MappingProxyType
from urllib.parse import parse_qs,urlsplit
from companion_memory.configuration.observation_settings import ObservationSettings
from companion_memory.runtime import RuntimeObserver
from companion_memory.runtime.results import Found,Failed
from companion_memory.logging_service import RuntimeLogReader,LogPage,LogGap,LogReadFailed

PAGE=b'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Runtime observations</title><h1>Runtime observations</h1><p>Actual local services; simulated model; synthetic results.</p><p>Read-only test session. Logs cover this process window only; file history is unavailable.</p><label>Entry ID (optional) <input id="entry" autocomplete="off"></label><main id="views"></main><script src="/status.js"></script></html>'''
SCRIPT=b'''"use strict";
const views=document.querySelector("#views");
for(const kind of ["runtime","entries","batches","logs"]){
  const section=document.createElement("section"),title=document.createElement("h2"),refresh=document.createElement("button"),next=document.createElement("button"),output=document.createElement("pre");
  title.textContent=kind;refresh.textContent="Refresh";next.textContent="Next page";next.disabled=true;output.textContent="Not observed";
  section.append(title,refresh,next,output);views.append(section);let cursor=null,entry=null;
  async function read(continuation){
    const selected=document.querySelector("#entry").value;
    if(!continuation||selected!==entry)cursor=null;entry=selected;
    const query=new URLSearchParams();if(entry&&kind!=="runtime")query.set("entry_id",entry);if(cursor)query.set("cursor",cursor);
    refresh.disabled=true;next.disabled=true;
    try{const response=await fetch("/api/observe/"+kind+"?"+query.toString(),{credentials:"same-origin",cache:"no-store"});const body=await response.json();output.textContent=JSON.stringify(body,null,2);cursor=response.ok?body.next_cursor:null;next.disabled=!(cursor&&body.has_more);}
    catch{output.textContent="UNAVAILABLE";cursor=null;}finally{refresh.disabled=false;}
  }
  refresh.addEventListener("click",()=>read(false));next.addEventListener("click",()=>read(true));
}
'''


def plain(value):
    if type(value) in (dict,MappingProxyType):return {key:plain(item) for key,item in value.items()}
    if type(value) in (list,tuple):return [plain(item) for item in value]
    if value is None or type(value) in (bool,int,float,str):return value
    raise TypeError('Only safe observation values may enter HTTP output.')


@dataclass(slots=True)
class _Session:
    runtime:RuntimeObserver | None
    logs:RuntimeLogReader | None
    expires:float
    reads:dict[str,float]


class ReadOnlyHTTP:
    """Bounded HTTP listener; authentication exists only for trusted local tests."""
    def __init__(self,settings:ObservationSettings):
        if type(settings) is not ObservationSettings:raise TypeError('Native observation settings are required.')
        self.settings=settings;self.sessions:dict[str,_Session]={};self.server:asyncio.Server | None=None
        self.tasks:set[asyncio.Task]=set();self.writers:set[asyncio.StreamWriter]=set();self.closed=False
    def issue_test_session(self,runtime:RuntimeObserver | None,logs:RuntimeLogReader | None,expires_at:float) -> str:
        if (runtime is not None and type(runtime) is not RuntimeObserver or logs is not None and type(logs) is not RuntimeLogReader
                or type(expires_at) is not float or not time.monotonic()<expires_at<float('inf') or len(self.sessions)>=self.settings.row_limit):raise ValueError('Finite native observation session capabilities are required.')
        token=secrets.token_urlsafe(32);self.sessions[token]=_Session(runtime,logs,expires_at,{})
        return token
    def revoke_test_session(self,token:str):self.sessions.pop(token,None)
    async def start(self) -> tuple[str,int]:
        if self.server is not None or self.closed:raise ValueError('HTTP listener is already initialized.')
        self.server=await asyncio.start_server(self.handle,'127.0.0.1',0,limit=self.settings.byte_limit)
        host,port=self.server.sockets[0].getsockname()[:2]
        return host,port
    async def send(self,writer:asyncio.StreamWriter,status:int,body:object,content_type:str='application/json'):
        encoded=body if type(body) is bytes else json.dumps(plain(body),ensure_ascii=False,separators=(',',':')).encode()
        if len(encoded)>self.settings.byte_limit:status=400;encoded=b'{"error":"INVALID_INPUT","reason":"LIMIT_EXCEEDED"}'
        labels={200:'OK',400:'Bad Request',401:'Unauthorized',403:'Forbidden',404:'Not Found',405:'Method Not Allowed',409:'Conflict',429:'Too Many Requests',503:'Service Unavailable'}
        header=f'HTTP/1.1 {status} {labels[status]}\r\nContent-Type: {content_type}\r\nContent-Length: {len(encoded)}\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Security-Policy: default-src \'self\'; frame-ancestors \'none\'\r\n\r\n'.encode()
        writer.write(header+encoded)
        await asyncio.wait_for(writer.drain(),self.settings.timeout_seconds)
    async def handle(self,reader:asyncio.StreamReader,writer:asyncio.StreamWriter):
        task=asyncio.current_task();assert task is not None
        if self.closed or len(self.tasks)>=self.settings.concurrency:
            try:await self.send(writer,429,{'error':'RESOURCE_BUSY'})
            finally:writer.close()
            return
        self.tasks.add(task);self.writers.add(writer)
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                block=await reader.readuntil(b'\r\n\r\n')
            if len(block)>self.settings.byte_limit:await self.send(writer,400,{'error':'INVALID_INPUT'});return
            try:
                lines=block.decode('ascii').split('\r\n');method,target,protocol=lines[0].split(' ')
                headers={}
                for line in lines[1:-2]:
                    key,value=line.split(':',1);key=key.lower()
                    if key in headers:raise ValueError()
                    headers[key]=value.strip()
                if protocol!='HTTP/1.1' or 'transfer-encoding' in headers or headers.get('content-length','0')!='0':raise ValueError()
                parsed=urlsplit(target)
                if parsed.scheme or parsed.netloc or parsed.fragment or not target.startswith('/'):raise ValueError()
            except (ValueError,UnicodeError):await self.send(writer,400,{'error':'INVALID_INPUT'});return
            if method!='GET':await self.send(writer,405,{'error':'METHOD_NOT_ALLOWED'});return
            token=headers.get('authorization','').removeprefix('Bearer ')
            if not token:
                for cookie in headers.get('cookie','').split(';'):
                    if cookie.strip().startswith('iris_observe='):token=cookie.strip().split('=',1)[1]
            session=self.sessions.get(token)
            if session is None or session.expires<=time.monotonic():await self.send(writer,401,{'error':'AUTHENTICATION_REQUIRED'});return
            if parsed.path in ('/status','/status.js'):
                if session.runtime is None and session.logs is None:await self.send(writer,403,{'error':'ACCESS_DENIED'});return
                await self.send(writer,200,PAGE if parsed.path=='/status' else SCRIPT,'text/html; charset=utf-8' if parsed.path=='/status' else 'text/javascript; charset=utf-8');return
            if parsed.path not in ('/api/observe/runtime','/api/observe/entries','/api/observe/batches','/api/observe/logs'):await self.send(writer,404,{'error':'NOT_FOUND'});return
            kind=parsed.path.rsplit('/',1)[1]
            if kind=='logs' and session.logs is None or kind!='logs' and session.runtime is None:await self.send(writer,403,{'error':'ACCESS_DENIED'});return
            now=time.monotonic()
            if now-session.reads.get(kind,float('-inf'))<self.settings.refresh_seconds:await self.send(writer,429,{'error':'REFRESH_LIMIT'});return
            try:
                values=parse_qs(parsed.query,keep_blank_values=True,strict_parsing=True,max_num_fields=16)
                if any(len(v)!=1 for v in values.values()):raise ValueError()
                args={k:v[0] for k,v in values.items()}
                if kind=='logs':
                    allowed={'cursor','limit','start','end','minimum_level','modules','event_codes','entry_id','run_id','request_id','attempt_id'}
                    if set(args)-allowed:raise ValueError()
                    query:dict[str,object]={k:args.get(k) for k in allowed}
                    query['limit']=int(args.get('limit',str(self.settings.log_row_limit)))
                    for k in ('modules','event_codes'):query[k]=args[k].split(',') if k in args else []
                else:
                    if set(args)-({'entry_id','cursor','limit'} | ({'batch_id','state'} if kind=='batches' else set())):raise ValueError()
                    query={'entry_id':args.get('entry_id'),'cursor':args.get('cursor'),'limit':int(args.get('limit',str(self.settings.row_limit)))}
                    if kind=='batches':query.update(batch_id=args.get('batch_id'),state=args.get('state'))
            except ValueError:await self.send(writer,400,{'error':'INVALID_INPUT'});return
            session.reads[kind]=now
            if kind=='logs':
                assert session.logs is not None
                result=await session.logs.query_runtime_logs(query)
            else:
                assert session.runtime is not None
                result=await {'runtime':session.runtime.read_runtime_view,'entries':session.runtime.read_entry_status,'batches':session.runtime.read_batch_status}[kind](query)
            if self.sessions.get(token) is not session:await self.send(writer,403,{'error':'ACCESS_DENIED'});return
            if type(result) is Found or type(result) is LogPage:
                value=result.value
                await self.send(writer,503 if type(value) is MappingProxyType and value.get('availability')=='UNAVAILABLE' else 200,value)
            elif type(result) is LogGap:await self.send(writer,409,{'error':'LOG_GAP','gap':result.value})
            else:
                failure=result.error if type(result) is Failed else result
                code=getattr(failure,'code','UNAVAILABLE');reason=getattr(failure,'reason','UNAVAILABLE')
                status=403 if code=='ACCESS_DENIED' else 400 if code in ('INVALID_INPUT','INVALID_QUERY') else 409 if code=='PRECONDITION_FAILED' else 429 if code=='RESOURCE_BUSY' else 503
                await self.send(writer,status,{'error':code,'reason':reason})
        except (asyncio.TimeoutError,asyncio.IncompleteReadError,asyncio.LimitOverrunError,ConnectionError,UnicodeError,ValueError):
            pass
        finally:
            writer.close()
            try:await asyncio.wait_for(writer.wait_closed(),self.settings.timeout_seconds)
            except (asyncio.TimeoutError,ConnectionError):pass
            self.writers.discard(writer);self.tasks.discard(task)
    async def close(self) -> bool:
        self.closed=True;self.sessions.clear()
        if self.server is not None:self.server.close();await self.server.wait_closed()
        for writer in tuple(self.writers):writer.close()
        if self.tasks:await asyncio.wait(tuple(self.tasks),timeout=self.settings.timeout_seconds)
        return not self.tasks
