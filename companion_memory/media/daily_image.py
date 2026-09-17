"""Owner-issued image bytes tied to actual PROCESSING references and full decode.

The Provider borrows this capability; it never reads paths or reconstructs a
media owner. Original file work and decode run on the existing media worker.
"""
from __future__ import annotations
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, stored_cognition_configuration_issue
import asyncio
from dataclasses import dataclass
from hashlib import sha256
import time
from typing import cast
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.owned_statements import BoundStatements,OwnerFailure
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.daily_records import Record
from companion_memory.persistence.content_codec import decode_content
from companion_memory.memory.formats import record
from companion_memory.provider.values import freeze,as_record
from .image_validation import CheckedImage,ImageRejected,ImageLimits,validate_image
from .processing_bytes import ProcessingBytes
from .service import MediaService,MediaError,identity

@dataclass(frozen=True,slots=True,init=False)
class DailyImageLease:
    owner:DailyImages
    image:CheckedImage
    work:Record
    blob:Record
    occurrence:Record
    reference:Record
    artifact_id:str
    deadline:float

class DailyImages:
    """One original image lease with its own actual completion registry."""
    def __init__(self,media:MediaService):
        from companion_memory.configuration.daily_persistence import StoredDailyConfiguration
        if type(media) is not MediaService or type(media.configuration) not in (StoredDailyConfiguration,StoredDreamConfiguration):raise InvalidValue()
        self.media=media;self.views=BoundStatements(media.catalog,media.storage,'provider');self._lease:DailyImageLease|None=None;self._task:asyncio.Task|None=None;self.closed=False
        self._rejection:tuple[Record,ImageRejected]|None=None

    async def acquire(self,work_id:str,deadline:float):
        if self.closed or self._lease is not None or self._task is not None or self._rejection is not None:raise OwnerFailure('RESOURCE_BUSY','media','CLEANUP_PENDING',True)
        abandoned=False
        async def read():
            m=self.media
            works=await m.rows.read('work_get',{'work_id':work_id})
            if len(works)!=1:raise InvalidValue()
            work=works[0]
            if work['modality']!='IMAGE' or work['task']!='DESCRIBE':raise OwnerFailure('INVALID_INPUT','media','INPUT_FORMAT_UNSUPPORTED')
            occurrence=(await m.rows.read('occurrences_get',{'occurrence_id':work['occurrence_id']}))[0]
            blob=(await m.rows.read('blobs_get',{'blob_id':work['blob_id']}))[0]
            ref=identity('media_ref','PROCESSING',work_id,cast(str,work['blob_id']),cast(int,work['generation']),cast(str,work['occurrence_id']))
            references=await m.rows.read('references_get',{'reference_id':ref})
            if len(references)!=1:raise InvalidValue()
            read=await m.read_processing(work_id,work['occurrence_id'])
            try:
                if type(read.result) is MediaError:raise OwnerFailure(read.result.code,read.result.field,read.result.reason,read.result.cleanup_pending)
                if type(read.result) is not ProcessingBytes:raise InvalidValue()
                data=read.result
                image=await m.integrity.io(blob,'image-decode:'+work_id,lambda:validate_image(data.content,ImageLimits(1048576,2048,4194304,1)))
                if type(image) is ImageRejected:
                    self._rejection=(work,image)
                    return image
                if type(image) is not CheckedImage:raise InvalidValue()
                descriptor=as_record(freeze(decode_content(cast(str,work['original_request_descriptor']).encode(),4096),4096,owned=True));original=as_record(as_record(descriptor['payload'])['media'])
                if (original['artifact_id']!=data.artifact_id or original['sha256']!=image.sha256 or original['byte_count']!=len(image.data)
                        or await m.rows.read('work_get',{'work_id':work_id})!=works or await m.rows.read('references_get',{'reference_id':ref})!=references):raise InvalidValue()
                if time.monotonic()>=deadline or self.closed:raise OwnerFailure('TIMEOUT','media','DEADLINE_EXCEEDED')
                lease=object.__new__(DailyImageLease)
                for key,value in dict(owner=self,image=image,work=work,blob=blob,occurrence=occurrence,reference=references[0],artifact_id=data.artifact_id,deadline=deadline).items():object.__setattr__(lease,key,value)
                self._lease=lease;return lease
            finally:await read.completion.wait()
        task,logical=start_owned(read());self._task=task
        def ended(job):
            failed=job.cancelled() or job.exception() is not None
            # A lease produced before a delayed physical completion was never
            # delivered to the timed-out caller. Only this actual task may drop
            # that orphan; logical timeout itself does not free its bytes.
            if (abandoned or failed) and self._lease is not None and self._lease.work['work_id']==work_id:self._lease=None
            if (abandoned or failed) and self._rejection is not None and self._rejection[0]['work_id']==work_id:self._rejection=None
            if self._task is job:self._task=None
        task.add_done_callback(ended)
        try:done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        except asyncio.CancelledError:
            abandoned=True
            if task.done() and self._lease is not None and self._lease.work['work_id']==work_id:self._lease=None
            if task.done() and self._rejection is not None and self._rejection[0]['work_id']==work_id:self._rejection=None
            raise
        if not done:
            abandoned=True
            if task.done() and self._lease is not None and self._lease.work['work_id']==work_id:self._lease=None
            if task.done() and self._rejection is not None and self._rejection[0]['work_id']==work_id:self._rejection=None
            raise OwnerFailure('TIMEOUT','media','DEADLINE_EXCEEDED',not task.done())
        return logical.result()

    def reader_active(self,lease:DailyImageLease) -> bool:
        """Observe this issued image without reading paths or releasing bytes."""
        if type(lease) is not DailyImageLease or lease.owner is not self:raise InvalidValue()
        return self._lease is lease

    def verify_rejection(self,uow,work_id:str,reason:str):
        """A local input failure belongs to the exact actual read and work revision."""
        if self._task is not None or self._rejection is None or self.closed:raise InvalidValue()
        work,failure=self._rejection
        if work['work_id']!=work_id or failure.reason!=reason:raise InvalidValue()
        current=self.media.rows.stage('work_get',uow,{'work_id':work_id})
        if len(current)!=1 or current[0]!=work:raise OwnerFailure('PRECONDITION_FAILED','media','OWNERSHIP_CHANGED')

    def release_rejection(self):
        if self._task is not None:raise OwnerFailure('RESOURCE_BUSY','media','CLEANUP_PENDING',True)
        self._rejection=None

    def verify(self,lease:DailyImageLease,uow=None):
        if self.closed or type(lease) is not DailyImageLease or lease is not self._lease or lease.owner is not self or sha256(lease.image.data).hexdigest()!=lease.image.sha256:raise InvalidValue()
        if uow is not None:
            work=record(lease.work)
            values=(('work',{'work_id':work['work_id']},{k:v for k,v in work.items() if k!='original_request_descriptor'}),
                ('work_descriptors',{'work_id':work['work_id'],'admission_generation':work['admission_generation']},{'work_id':work['work_id'],'admission_generation':work['admission_generation'],'body':work['original_request_descriptor']}),
                ('blobs',{'blob_id':record(lease.blob)['blob_id']},lease.blob),('occurrences',{'occurrence_id':record(lease.occurrence)['occurrence_id']},lease.occurrence),
                ('references',{'reference_id':record(lease.reference)['reference_id']},lease.reference))
            for name,keys,expected in values:
                rows=self.views.stage('daily_provider_'+name,uow,{'caller_scope':self.media.instance_id,**keys})
                if len(rows)!=1 or rows[0]!=expected:raise OwnerFailure('PRECONDITION_FAILED','media','OWNERSHIP_CHANGED')
        return lease.image

    def release(self,lease:DailyImageLease):
        if self._task is not None or lease is not self._lease:raise InvalidValue()
        self._lease=None

    async def wait_actual(self):
        if self._task is not None:await asyncio.wait((self._task,))

    def close(self):
        self.closed=True
        if self._task is None:self._rejection=None
        return self._task is None and self._lease is None
