"""Exclusive generation file ownership, sealing and bounded reader retirement.

The database owner must establish build or retirement intent before calling the
corresponding file operation. This private file owner never publishes a database
pointer. Actual readers retain mappings and directory ownership until they exit.
"""
from collections.abc import Callable,Iterable
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import mmap
import os
from pathlib import Path
import stat
import threading
from typing import Iterator
from .semantic_binary import (VectorHeader,VectorMember,InvalidVectorFile,write_generation,read_members,
    FILE_LIMIT,HEADER_BYTES,RECORD_BYTES,MEMBER_LIMIT)


@dataclass(frozen=True,slots=True,init=False)
class WrittenVectorPage:
    """Original file-owner proof of one complete fsynced record range."""
    generation_id: str
    page_no: int
    offset: int
    member_count: int
    page_digest: str
    _issuer: object

    def __init__(self) -> None:
        raise TypeError('Page evidence is issued only by the bound file owner.')


@dataclass(frozen=True,slots=True,init=False)
class SealedGeneration:
    """File-owner evidence after full validation, fsync, rename and directory sync."""
    generation_id: str
    header: VectorHeader
    file_bytes: int
    file_digest: str
    _issuer: object

    def __init__(self) -> None:
        raise TypeError('Obtain original evidence from the bound vector file owner.')


class VectorFiles:
    """One exclusively locked root, two generation files and two real readers."""
    def __init__(self,root: Path,*,file_limit_bytes: int,index_total_bytes: int,reader_limit: int):
        if (type(root) is not Path and not isinstance(root,Path) or not root.is_absolute()
                or file_limit_bytes!=41943040 or index_total_bytes!=83886080 or reader_limit!=2):
            raise InvalidVectorFile('Unsupported vector resource binding.')
        for part in (root,*root.parents):
            if part.is_symlink(): raise InvalidVectorFile('Symbolic vector directory is forbidden.')
        root.mkdir(mode=0o700,exist_ok=True)
        self._root=root;self._lock=threading.RLock();self._readers: dict[str,int]={};self._closing=False
        self._closed=False;self._building=False;self._retiring: str | None=None
        self._issuer=object()
        self._dir_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            self._owner_fd=os.open('.owner',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=self._dir_fd)
            ownership=os.fstat(self._owner_fd)
            if not stat.S_ISREG(ownership.st_mode) or ownership.st_nlink!=1:
                raise InvalidVectorFile('Invalid vector ownership file.')
            fcntl.flock(self._owner_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:
            os.close(self._dir_fd)
            if hasattr(self,'_owner_fd'): os.close(self._owner_fd)
            raise
        self._directory_identity=(os.fstat(self._dir_fd).st_dev,os.fstat(self._dir_fd).st_ino)
        self._file_limit=file_limit_bytes;self._total_limit=index_total_bytes;self._reader_limit=reader_limit

    @staticmethod
    def _name(generation_id: str) -> str:
        from .semantic_binary import _domain
        _domain(generation_id,'semantic-generation')
        return generation_id

    def _ready(self) -> None:
        if self._closing or self._closed: raise InvalidVectorFile('Vector owner is closing.')
        current=os.stat(self._root,follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev,current.st_ino)!=self._directory_identity:
            raise InvalidVectorFile('Vector directory ownership changed.')

    def _inventory(self) -> tuple[dict[str,int],int]:
        files={};total=0
        for name in os.listdir(self._dir_fd):
            info=os.stat(name,dir_fd=self._dir_fd,follow_symlinks=False)
            if name=='.owner':continue
            base=name.removesuffix('.building')
            self._name(base)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>self._file_limit:
                raise InvalidVectorFile('Unexpected vector directory member.')
            files[name]=info.st_size;total+=info.st_size
        if len(files)>2 or total>self._total_limit: raise InvalidVectorFile('Vector directory capacity reached.')
        return files,total

    def _reset_damaged(self,space_id:str,generation_id:str,expected_digest:str,checkpoint:Callable[[],None]) -> bool:
        """Retain valid files; discard only a damaged native generation after proof.

        The generation owner first verifies every retained paid artifact. An
        actual reader prevents this operation even when a new read would fail.
        """
        with self._lock:
            self._ready();checkpoint();name=self._name(generation_id)
            if self._building or self._readers.get(generation_id):raise InvalidVectorFile('A real generation consumer remains.')
            try:
                sealed=self._verify_locked(space_id,generation_id,checkpoint)
                if sealed.file_digest==expected_digest:return False
            except (InvalidVectorFile,FileNotFoundError):pass
            for target in (name,name+'.building'):
                try:metadata=os.stat(target,dir_fd=self._dir_fd,follow_symlinks=False)
                except FileNotFoundError:continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1 or metadata.st_size>self._file_limit:raise InvalidVectorFile('Unowned rebuild resource.')
                os.unlink(target,dir_fd=self._dir_fd)
            os.fsync(self._dir_fd);return True

    def seal(self,space_id: str,generation_id: str,captured_seq: int,members: Iterable[VectorMember],
             checkpoint: Callable[[],None]) -> SealedGeneration:
        """Write one already claimed generation; never replace an existing file.

        A prior sealed file is verified and returned unchanged. A partial build
        remains owned for recovery if interrupted; callers retain the original
        database command and must confirm it before publishing or discarding it.
        """
        name=self._name(generation_id)
        with self._lock:
            self._ready()
            if self._building: raise InvalidVectorFile('A vector builder is already active.')
            self._building=True
        try:
            files,_=self._inventory()
            if name in files:
                sealed=self.verify(space_id,generation_id,checkpoint)
                if sealed.header.captured_seq!=captured_seq:
                    raise InvalidVectorFile('Original generation capture differs.')
                # Recovery must still bind the exact original membership. It
                # cannot adopt a different file merely because its ID matches.
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self._dir_fd)
                with os.fdopen(fd,'rb') as original:
                    expected=iter(members)
                    for stored in read_members(original,sealed.header,checkpoint):
                        supplied=next(expected,None)
                        if supplied is None or supplied!=stored:
                            raise InvalidVectorFile('Original generation members differ.')
                    if next(expected,None) is not None:
                        raise InvalidVectorFile('Original generation members differ.')
                return sealed
            temporary=name+'.building'
            if temporary not in files and len(files)>=2: raise InvalidVectorFile('Both generation slots are occupied.')
            if self._readers.get(generation_id): raise InvalidVectorFile('A reader retains the generation.')
            flags=os.O_RDWR|os.O_NOFOLLOW|os.O_CREAT
            fd=os.open(temporary,flags,0o600,dir_fd=self._dir_fd)
            with os.fdopen(fd,'w+b') as file:
                if os.fstat(file.fileno()).st_nlink!=1: raise InvalidVectorFile('Aliased vector build file.')
                header=write_generation(file,space_id,generation_id,captured_seq,members,checkpoint)
                count=sum(1 for _ in read_members(file,header,checkpoint))
                if count!=header.member_count: raise InvalidVectorFile('Incomplete vector build.')
                os.fsync(file.fileno())
            with self._lock:
                self._ready();checkpoint()
                os.rename(temporary,name,src_dir_fd=self._dir_fd,dst_dir_fd=self._dir_fd)
                os.fsync(self._dir_fd)
            return self.verify(space_id,generation_id,checkpoint)
        finally:
            with self._lock:self._building=False

    def stage_page(self,space_id: str,generation_id: str,captured_seq: int,page_no: int,
                   members: tuple[VectorMember,...],checkpoint: Callable[[],None]) -> WrittenVectorPage:
        """Append one frozen database page, or confirm its identical bytes.

        A torn original page resumes only after checking every retained byte.
        Holes and conflicting prefixes fail closed. No database pointer is
        changed, and this evidence cannot authorize a different file owner.
        """
        if type(page_no) is not int or not 0<=page_no<512 or type(members) is not tuple or not 1<=len(members)<=8:
            raise InvalidVectorFile('Invalid vector page extent.')
        name=self._name(generation_id);temporary=name+'.building'
        provisional=VectorHeader(space_id,generation_id,captured_seq,0,sha256(b'[]').digest(),sha256(b'').digest()).encode()
        raw=b''.join(member.encode() for member in members)
        if any(member.applied_seq>captured_seq for member in members) or any(a.object_id>=b.object_id for a,b in zip(members,members[1:])):
            raise InvalidVectorFile('Invalid captured page membership.')
        offset=HEADER_BYTES+page_no*8*RECORD_BYTES
        with self._lock:
            self._ready();checkpoint()
            if self._building:raise InvalidVectorFile('A vector builder is already active.')
            files,total=self._inventory()
            if name in files:raise InvalidVectorFile('A sealed generation cannot accept a page.')
            if temporary not in files and (page_no or len(files)>=2):raise InvalidVectorFile('No original sequential build slot.')
            fd=os.open(temporary,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=self._dir_fd)
            with os.fdopen(fd,'r+b') as file:
                size=os.fstat(fd).st_size
                if size==0:
                    if page_no:raise InvalidVectorFile('Missing original build header.')
                    checkpoint()
                    if file.write(provisional)!=HEADER_BYTES:raise OSError('Incomplete provisional header write.')
                    size=HEADER_BYTES
                file.seek(0)
                if file.read(HEADER_BYTES)!=provisional:raise InvalidVectorFile('Original build header differs.')
                if size<offset or size>offset+len(raw) and (len(members)<8 or size%RECORD_BYTES!=HEADER_BYTES%RECORD_BYTES):
                    raise InvalidVectorFile('A page would create a hole or replace a later torn page.')
                if offset+len(raw)>self._file_limit or total-files.get(temporary,0)+max(size,offset+len(raw))>self._total_limit:
                    raise InvalidVectorFile('Vector directory capacity reached.')
                if page_no:
                    file.seek(offset-RECORD_BYTES);previous=VectorMember.decode(file.read(RECORD_BYTES))
                    if previous.object_id>=members[0].object_id:raise InvalidVectorFile('Page ordering differs.')
                file.seek(offset);retained=file.read(len(raw))
                if retained!=raw[:len(retained)]:raise InvalidVectorFile('Original page bytes differ.')
                file.seek(offset+len(retained));remaining=raw[len(retained):]
                checkpoint()
                if remaining and file.write(remaining)!=len(remaining):raise OSError('Incomplete vector page write.')
                file.flush();checkpoint();os.fsync(fd);checkpoint();os.fsync(self._dir_fd)
            evidence=object.__new__(WrittenVectorPage)
            for key,value in {'generation_id':generation_id,'page_no':page_no,'offset':offset,'member_count':len(members),
                'page_digest':sha256(raw).hexdigest(),'_issuer':self._issuer}.items():object.__setattr__(evidence,key,value)
            return evidence

    def confirm_written_page(self,evidence: WrittenVectorPage,checkpoint: Callable[[],None]) -> None:
        """Recheck retained native page evidence against its actual current range."""
        with self._lock:
            self._ready();checkpoint()
            if type(evidence) is not WrittenVectorPage or getattr(evidence,'_issuer',None) is not self._issuer:
                raise InvalidVectorFile('Foreign page evidence.')
            name=self._name(evidence.generation_id)
            files,_=self._inventory();path=name if name in files else name+'.building'
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self._dir_fd)
            with os.fdopen(fd,'rb') as file:
                file.seek(evidence.offset);raw=file.read(evidence.member_count*RECORD_BYTES)
            if len(raw)!=evidence.member_count*RECORD_BYTES or sha256(raw).hexdigest()!=evidence.page_digest:
                raise InvalidVectorFile('Confirmed page bytes changed.')

    def seal_staged(self,space_id: str,generation_id: str,captured_seq: int,member_count: int,
                    member_digest: str,checkpoint: Callable[[],None]) -> SealedGeneration:
        """Seal exactly the persisted membership after all page confirmations.

        The caller checks database page states. This owner independently checks
        every record, identity, length and digest before durable publication of
        the file. An interrupted header write requires paid-artifact rebuilding.
        """
        if (type(member_count) is not int or not 0<=member_count<=MEMBER_LIMIT or type(member_digest) is not str
                or len(member_digest)!=64 or any(c not in '0123456789abcdef' for c in member_digest)
                or not member_count and member_digest!=sha256(b'[]').hexdigest()):
            raise InvalidVectorFile('Invalid final generation size.')
        name=self._name(generation_id);temporary=name+'.building'
        with self._lock:
            self._ready();checkpoint()
            if self._building:raise InvalidVectorFile('A vector builder is already active.')
            files,_=self._inventory()
            if name in files:
                result=self._verify_locked(space_id,generation_id,checkpoint)
                if (result.header.captured_seq,result.header.member_count,result.header.member_digest.hex())!=(captured_seq,member_count,member_digest):
                    raise InvalidVectorFile('Original sealed membership differs.')
                return result
            if temporary not in files:
                if member_count:raise InvalidVectorFile('Missing staged generation.')
                return self.seal(space_id,generation_id,captured_seq,(),checkpoint)
            fd=os.open(temporary,os.O_RDWR|os.O_NOFOLLOW,dir_fd=self._dir_fd)
            with os.fdopen(fd,'r+b') as file:
                if os.fstat(fd).st_size!=HEADER_BYTES+member_count*RECORD_BYTES:
                    raise InvalidVectorFile('Staged record extent differs.')
                prior=VectorHeader.decode(file.read(HEADER_BYTES),space_id,generation_id)
                if prior.captured_seq!=captured_seq:raise InvalidVectorFile('Captured sequence differs.')
                bindings=sha256(b'[');records=sha256();previous=''
                for ordinal in range(member_count):
                    if ordinal%8==0:checkpoint()
                    raw=file.read(RECORD_BYTES);member=VectorMember.decode(raw)
                    if member.object_id<=previous or member.applied_seq>captured_seq:
                        raise InvalidVectorFile('Staged member ordering differs.')
                    records.update(raw)
                    if ordinal:bindings.update(b',')
                    bindings.update(member.binding_bytes());previous=member.object_id
                bindings.update(b']')
                if bindings.hexdigest()!=member_digest:raise InvalidVectorFile('Staged membership digest differs.')
                header=VectorHeader(space_id,generation_id,captured_seq,member_count,bindings.digest(),records.digest())
                file.seek(0)
                checkpoint()
                if file.write(header.encode())!=HEADER_BYTES:raise OSError('Incomplete sealed header write.')
                file.flush();checkpoint();os.fsync(fd)
            checkpoint();os.rename(temporary,name,src_dir_fd=self._dir_fd,dst_dir_fd=self._dir_fd);checkpoint();os.fsync(self._dir_fd)
            return self._verify_locked(space_id,generation_id,checkpoint)

    def verify(self,space_id: str,generation_id: str,checkpoint: Callable[[],None]) -> SealedGeneration:
        """Read the complete existing file without changing publication or vectors."""
        # Verification retains the same lock as retirement/close. Neither may
        # release resources while a full-file integrity consumer is active.
        with self._lock:
            return self._verify_locked(space_id,generation_id,checkpoint)

    def _verify_locked(self,space_id: str,generation_id: str,checkpoint: Callable[[],None]) -> SealedGeneration:
        name=self._name(generation_id)
        self._ready()
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self._dir_fd)
        with os.fdopen(fd,'rb') as file:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not 4096<=info.st_size<=FILE_LIMIT:
                raise InvalidVectorFile('Invalid sealed vector file.')
            header=VectorHeader.decode(file.read(4096),space_id,generation_id)
            for _ in read_members(file,header,checkpoint): pass
            file.seek(0);digest=sha256()
            while raw:=file.read(65536):checkpoint();digest.update(raw)
            evidence=object.__new__(SealedGeneration)
            for key,value in {'generation_id':generation_id,'header':header,'file_bytes':info.st_size,
                              'file_digest':digest.hexdigest(),'_issuer':self._issuer}.items():
                object.__setattr__(evidence,key,value)
            return evidence

    @contextmanager
    def reader(self,sealed: SealedGeneration) -> Iterator[mmap.mmap]:
        """Retain the actual mapping until the calculation and delivery finish."""
        with self._lock:
            self._ready()
            if type(sealed) is not SealedGeneration or getattr(sealed,'_issuer',None) is not self._issuer:
                raise InvalidVectorFile('Foreign or fabricated sealed evidence.')
            if sealed.generation_id==self._retiring:raise InvalidVectorFile('Generation reader admission ended.')
            if sum(self._readers.values())>=self._reader_limit: raise InvalidVectorFile('Vector reader capacity reached.')
            name=self._name(sealed.generation_id)
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self._dir_fd)
            try:
                info=os.fstat(fd)
                if info.st_size!=sealed.file_bytes or info.st_nlink!=1: raise InvalidVectorFile('Sealed file identity changed.')
                mapped=mmap.mmap(fd,0,access=mmap.ACCESS_READ)
                if sha256(mapped).hexdigest()!=sealed.file_digest:
                    mapped.close()
                    raise InvalidVectorFile('Sealed file content changed.')
            finally:os.close(fd)
            self._readers[name]=self._readers.get(name,0)+1
        try: yield mapped
        finally:
            mapped.close()
            with self._lock:
                self._readers[name]-=1
                if not self._readers[name]:del self._readers[name]

    def retire(self,generation_id: str) -> bool:
        """Finish a database-authorized retirement only when real readers ended.

        The caller owns the persisted RETIRING/FAILED intent. False means actual
        resources remain retained; it must not mark the generation RETIRED.
        """
        with self._lock:
            self._ready();name=self._name(generation_id)
            if self._readers.get(name) or self._building:return False
            for path in (name,name+'.building'):
                try:
                    info=os.stat(path,dir_fd=self._dir_fd,follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise InvalidVectorFile('Retirement file ownership differs.')
                    os.unlink(path,dir_fd=self._dir_fd)
                except FileNotFoundError:pass
            os.fsync(self._dir_fd)
            return True

    def retirement_ready(self,generation_id: str) -> bool:
        """Stop new readers of one persisted RETIRING/FAILED generation.

        The database owner must establish that intent before this call. Existing
        readers retain their actual mappings and keep collection inadmissible.
        """
        with self._lock:
            self._ready();name=self._name(generation_id)
            if self._retiring is not None and self._retiring!=name:
                files,_=self._inventory()
                if self._retiring in files or self._retiring+'.building' in files:
                    raise InvalidVectorFile('Another generation is still retiring.')
            self._retiring=name
            return not self._readers.get(name) and not self._building

    def close(self) -> bool:
        """Close admission immediately, retaining directory lock for active work."""
        with self._lock:
            if self._closed:return True
            self._closing=True
            if self._building or self._readers:return False
            os.close(self._owner_fd);os.close(self._dir_fd);self._closed=True
            return True
