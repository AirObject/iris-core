"""Persistent whole-instance operation admission and actual owned-volume checks.

The append-only admission journal is written before a new transaction handler.
Rollback does not refund it. Original receipt reads consume no write admission.
Completion reserve is limited to native original-work commands and is separate
from ordinary operation capacity; it never grants a new model request.
"""
from __future__ import annotations
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import stat
import threading
import time
from types import MappingProxyType
from companion_memory.configuration.daily_resolution import DailyConfigurationCandidate,daily_snapshot_issue
from companion_memory.configuration.dream_resolution import DreamConfigurationCandidate,dream_snapshot_issue
from companion_memory.configuration.semantic_resolution import SemanticConfigurationCandidate,semantic_snapshot_issue
from companion_memory.configuration import PresentValue
from .content_codec import encode_content,decode_content
from .semantic_records import Record,number,string,isolate,record,ID,N,H,enum
from .schema import InvalidValue,BoundedTextSchema,SequenceSchema
from .results import RecoveryHandle
from .deadlines import check_deadline
from .owned_statements import OwnerFailure

ENTRY=record(v=N,ordinal=N,pool=enum('NORMAL','COMPLETION'),owner=ID,kind=ID,scope=ID,key=ID,fingerprint=H,previous=H,
    original_values=(SequenceSchema(BoundedTextSchema(65536),1,128),))
MAX_ENTRY_BYTES=8388608
COMPLETION=frozenset(('apply','record_result','record_cleanup','fail','supersede','confirm_embedding_handoff',
    'store_embedding_handoff','retire_embedding_handoff','retire_page','generation_fail','cache_bind','cache_expire','gc_page','pause'))


class SemanticAdmission:
    """One exclusive persistent counter covering every registered business owner."""
    def __init__(self,configuration: SemanticConfigurationCandidate | DailyConfigurationCandidate | DreamConfigurationCandidate,root: Path,mode: str):
        issue=dream_snapshot_issue(configuration) if type(configuration) is DreamConfigurationCandidate else daily_snapshot_issue(configuration) if type(configuration) is DailyConfigurationCandidate else semantic_snapshot_issue(configuration)
        if issue is not None or mode not in ('CREATE_NEW','OPEN_EXISTING'):raise InvalidValue()
        if not root.is_absolute() or root.resolve(strict=True)!=root or any(p.is_symlink() for p in (root,*root.parents)):raise InvalidValue()
        self.dream=type(configuration) is DreamConfigurationCandidate
        self.daily=type(configuration) in (DailyConfigurationCandidate,DreamConfigurationCandidate)
        self.format='DREAM_OPERATION_ADMISSION_V1' if self.dream else 'DAILY_OPERATION_ADMISSION_V1' if self.daily else 'SEMANTIC_OPERATION_ADMISSION_V1'
        self.root=root;self.settings=configuration.text.record('retrieval.semantic_storage');self._lock=threading.RLock()
        values={e.definition.key:e.state.value for e in configuration.foundation.list_entries() if type(e.state) is PresentValue}
        database=values['storage.database_file'];assert type(database) is str
        self.database=Path(database)
        staging=configuration.content.value('media.staging_directory');assert type(staging) is str
        self.staging=Path(staging)
        self.media_published=Path(str(configuration.content.value('media.root_directory')))/'published'
        log_directory=values['logging.file_directory'];assert type(log_directory) is str
        if not self.staging.is_relative_to(root) or not Path(log_directory).is_relative_to(root):raise InvalidValue()
        if not self.database.is_relative_to(root) or not Path(string(self.settings['index_root'])).is_relative_to(root):raise InvalidValue()
        self._fd=-1;self._normal=0;self._completion=0;self._digest='0'*64;self._faulted=False
        self._originals:dict[tuple[str,str,str,str],tuple[int,int]]={}
        self._identity=sha256(encode_content(MappingProxyType({'settings':self.settings,'database':str(self.database)}),16384)).hexdigest()
        self.checkpoint()
        flags=os.O_RDWR|os.O_APPEND|os.O_NOFOLLOW|(os.O_CREAT|os.O_EXCL if mode=='CREATE_NEW' else 0)
        fd=os.open(root/'semantic-admission.jsonl',flags,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink!=1:raise InvalidValue()
            self._fd=fd
            if mode=='CREATE_NEW':
                header=encode_content(MappingProxyType({'format':self.format,'binding':self._identity}),1024)+b'\n'
                if os.write(fd,header)!=len(header):raise OSError('Admission header write incomplete.')
                os.fsync(fd)
                directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(directory)
                finally:os.close(directory)
            self._recover()
            if mode=='CREATE_NEW':self._append('persistence','CREATE_NEW','local',self._identity,self._identity,False)
        except BaseException:
            os.close(fd);self._fd=-1;raise

    def _recover(self) -> None:
        deadline=time.monotonic()+5
        with os.fdopen(os.dup(self._fd),'rb') as stream:
            stream.seek(0);header=stream.readline(1025)
            expected=encode_content(MappingProxyType({'format':self.format,'binding':self._identity}),1024)+b'\n'
            if header!=expected:raise InvalidValue()
            ordinal=0
            offset=stream.tell()
            while raw:=stream.readline(MAX_ENTRY_BYTES+1):
                if time.monotonic()>=deadline:raise TimeoutError('Admission recovery deadline.')
                if len(raw)>MAX_ENTRY_BYTES or not raw.endswith(b'\n'):raise InvalidValue()
                entry=isolate(ENTRY,decode_content(raw[:-1],MAX_ENTRY_BYTES),MAX_ENTRY_BYTES)
                if entry['v']!=1 or entry['ordinal']!=ordinal+1 or entry['previous']!=self._digest or encode_content(entry,MAX_ENTRY_BYTES)+b'\n'!=raw:raise InvalidValue()
                self._remember(entry,offset,len(raw));offset=stream.tell()
                self._normal+=int(entry['pool']=='NORMAL');self._completion+=int(entry['pool']=='COMPLETION')
                self._digest=sha256(raw).hexdigest();ordinal+=1
                if self._normal>number(self.settings['normal_operation_limit']) or self._completion>number(self.settings['completion_operation_reserve']):raise InvalidValue()

    def checkpoint(self) -> None:
        """Measure complete owned files and their actual volume; unknown stops work."""
        if self._faulted:raise OwnerFailure('RESOURCE_BUSY','storage','CLEANUP_PENDING',True)
        deadline=time.monotonic()+1;total=database=wal=backup=temporary=index=0
        aliases={};physical=set()
        def files(directory: Path):
            if time.monotonic()>=deadline:raise TimeoutError('Resource observation incomplete.')
            with os.scandir(directory) as entries:
                for entry in entries:
                    if time.monotonic()>=deadline:raise TimeoutError('Resource observation incomplete.')
                    st=entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):yield from files(Path(entry.path))
                    elif stat.S_ISREG(st.st_mode):
                        path=Path(entry.path);inode=(st.st_dev,st.st_ino)
                        if st.st_nlink!=1:
                            if not self.daily or path.parent not in (self.staging,self.media_published):raise OSError('Owned resource alias or special file.')
                            paths,links=aliases.setdefault(inode,([],st.st_nlink))
                            if links!=st.st_nlink:raise OSError('Media publication changed during observation.')
                            paths.append(path)
                        yield path,st.st_size,inode
                    else:raise OSError('Owned resource alias or special file.')
        try:
            for path,size,inode in files(self.root):
                if inode not in physical:total+=size;physical.add(inode)
                if path==self.database:database+=size
                elif str(path)==str(self.database)+'-wal':wal+=size
                elif path.is_relative_to(self.root/'backup'):backup+=size
                elif path.is_relative_to(Path(string(self.settings['index_root']))):index+=size
                elif path.is_relative_to(self.root/'temporary') or path.is_relative_to(self.staging):temporary+=size
            for paths,links in aliases.values():
                if len(paths)!=links or sum(path.parent==self.media_published for path in paths)!=1 or not any(path.parent==self.staging for path in paths):
                    raise OSError('Media publication links escape the owned publication pair.')
            free=os.statvfs(self.root)
            if (free.f_bavail*free.f_frsize<number(self.settings['free_reserve_bytes'])
                    or any(actual>=number(self.settings[key]) for actual,key in ((total,'directory_stop_bytes'),(database,'database_stop_bytes'),
                        (wal,'wal_stop_bytes'),(backup,'backup_stop_bytes'),(temporary,'temporary_stop_bytes'),(index,'index_total_bytes')))):
                raise OSError('Semantic resource stop reached.')
        except (OSError,TimeoutError):raise OwnerFailure('RESOURCE_BUSY','storage','CAPACITY_REACHED') from None

    def admit(self,handle: RecoveryHandle,values: Record) -> None:
        """Count one actual write attempt after reliable original receipt absence."""
        kind=handle.identity.operation_kind;eligible=kind in COMPLETION
        daily_completion=('store_daily_handoff','confirm_daily_handoff','retire_daily_handoff','store_goal_comparison','apply_goal_comparison','finish_goal_comparison',
            'associate_daily_image','store_daily_image','retire_daily_image_processing','complete_trigger','retire_reasoning_material',
            'expire_goal_comparison','retire_goal_material',
            'associate_reasoning_turn','store_reasoning_result','finish_reasoning_turn','store_reasoning_tool','stage_daily_candidate','stage_daily_candidate_media','plan_daily_candidate')
        daily_application=tuple('apply_daily_candidate'+('_history' if h else '')+('_media' if m else '')+('_goals' if g else '')
            for h in (False,True) for m in (False,True) for g in (False,True))
        if self.daily and kind in daily_completion+daily_application:eligible=True
        dream_completion=('request_dream_abort','pause_dream','abort_background_dream','exit_focused_dream','finish_focused_dream',
            'finish_background_dream','complete_dream_exit','store_dream_review_result','record_dream_review_failure','finish_dream_review',
            'discard_dream_review','retire_dream_material','finish_dream_influence','discard_dream_influence',
            'store_periodic_generation','store_periodic_review','store_periodic_failure','record_periodic_failure','record_periodic_unknown',
            'keep_periodic_persona','discard_periodic_persona','retire_periodic_material')
        if self.dream and kind in dream_completion:eligible=True
        if kind=='prepare' and type(values.get('payload')) is str:
            payload=decode_content(string(values['payload']).encode(),24576)
            eligible=type(payload) is dict and payload.get('kind')=='DELETE_LOCAL'
        if handle.identity.owner_namespace=='provider' and kind in ('evidence','recover','settle','terminate'):
            changes=values['changes'];eligible=type(changes) is tuple and all(type(c) is MappingProxyType and (c['expected_revision'] is not None or c['table']=='cost_items') for c in changes) and any(type(c) is MappingProxyType and c['table']=='attempts' and c['expected_revision'] is not None for c in changes)
        self._append(handle.identity.owner_namespace,kind,handle.identity.scope_id,handle.identity.operation_key,handle.fingerprint,eligible,values)

    def _append(self,owner: str,kind: str,scope: str,key: str,fingerprint: str,eligible: bool,values:Record|None=None) -> None:
        with self._lock:
            self.checkpoint();check_deadline()
            if self._fd<0:raise OwnerFailure('INVALID_STATE','storage','SERVICE_CLOSED')
            pool='NORMAL' if self._normal<number(self.settings['normal_operation_limit']) else 'COMPLETION'
            if pool=='COMPLETION' and (not eligible or self._completion>=number(self.settings['completion_operation_reserve'])):
                raise OwnerFailure('RESOURCE_BUSY','storage','CAPACITY_REACHED')
            body=None if values is None else encode_content(values,2097152).decode()
            entry=isolate(ENTRY,{'v':1,'ordinal':self._normal+self._completion+1,'pool':pool,'owner':owner,'kind':kind,'scope':scope,
                'key':key,'fingerprint':fingerprint,'previous':self._digest,
                'original_values':None if body is None else tuple(body[n:n+16384] for n in range(0,len(body),16384))},MAX_ENTRY_BYTES)
            existing=self.original(owner,kind,scope,key)
            if existing is not None:
                from companion_memory.ingress.events import plain
                if plain(values)!=existing:raise InvalidValue()
            raw=encode_content(entry,MAX_ENTRY_BYTES)+b'\n';offset=os.fstat(self._fd).st_size
            check_deadline()
            try:
                if os.write(self._fd,raw)!=len(raw):raise OSError('Admission write incomplete.')
                os.fsync(self._fd)
            except OSError:
                self._faulted=True
                raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE') from None
            self._normal+=int(pool=='NORMAL');self._completion+=int(pool=='COMPLETION');self._digest=sha256(raw).hexdigest()
            self._remember(entry,offset,len(raw))

    def _remember(self,entry:Record,offset:int,length:int) -> None:
        if entry['original_values'] is not None:
            key=tuple(string(entry[k]) for k in ('owner','kind','scope','key'))
            self._originals.setdefault((key[0],key[1],key[2],key[3]),(offset,length))

    def original(self,owner:str,kind:str,scope:str,key:str) -> object:
        """Restore the first submitted values without resetting a deadline or key."""
        location=self._originals.get((owner,kind,scope,key))
        if location is None:return None
        offset,length=location;raw=os.pread(self._fd,length,offset)
        entry=isolate(ENTRY,decode_content(raw[:-1],MAX_ENTRY_BYTES),MAX_ENTRY_BYTES)
        parts=entry['original_values'];assert type(parts) is tuple
        return decode_content(''.join(string(p) for p in parts).encode(),2097152)

    def observe(self) -> Record:
        """Expose only cumulative admission facts, never resettable counters."""
        return MappingProxyType({'normal':self._normal,'completion':self._completion,'digest':self._digest,'faulted':self._faulted})

    def admit_external(self,kind: str,scope: str,key: str,fingerprint: str) -> None:
        """Count an outer authorization append separately from native ledger work."""
        self._append('semantic_authorization',kind,scope,key,fingerprint,kind=='complete_slot')

    def close(self) -> None:
        """The storage owner calls after all actual writers and local jobs end."""
        with self._lock:
            if self._fd>=0:os.close(self._fd);self._fd=-1
