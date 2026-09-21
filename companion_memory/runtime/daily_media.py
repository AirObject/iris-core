"""Native daily image preparation on the shared Provider and physical network.

Existing occurrence selection and source protection keep their media ownership.
Only the original role-bound Provider key is sent, and original recovery always
passes fresh=False. Actual bytes and result consumers finish before retirement.
"""
from __future__ import annotations
import asyncio
from hashlib import sha256
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Committed,Found,NotFound
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.content_codec import encode_content
from companion_memory.memory.formats import record,sequence
from companion_memory.media.daily_image import DailyImages,DailyImageLease
from companion_memory.media.image_validation import ImageRejected
from companion_memory.media.provider_source import image_input
from companion_memory.provider.media_input import ImageInput
from companion_memory.provider.daily_service import DailyProvider
from .content_media import ContentMedia,MediaPolicy

class DailyMedia(ContentMedia):
    """Reuse native FIFO preparation with explicit daily dispatch and reception."""
    def __init__(self,runtime,image_work,admitted):
        if type(runtime.provider) is not DailyProvider or not runtime.assembly.daily_format:raise InvalidValue()
        self.runtime=runtime;self.media=runtime.assembly.media;self.provider=runtime.provider;self.image_work=image_work;self.admitted=admitted
        if self.media is None:raise InvalidValue()
        settings=runtime.assembly.configuration.candidate.text.record('media.image_understanding')
        self.policy=MediaPolicy(runtime.assembly.instance_id,cast(str,settings['profile_id']),cast(str,settings['prompt_ref']))
        self.images=DailyImages(self.media);self.closed=False;self._sending=None;self._task=None
        self._sending_input: ImageInput | None = None
        self.media.integrity.notify=runtime.gate.invalidate_current
        image_work.bind(self.provider)
        image_work.images=self.images

    def fingerprint(self):
        role=self.provider.bindings['MEDIA']
        return sha256(encode_content(MappingProxyType({'model':role.requested_model,'prompt':role.prompt_digest,'schema':role.schema_digest,'scope':self.policy.authorization_domain_id}),2048)).hexdigest()

    def release_ended_capabilities(self):
        """Each owned drive releases its own image lease after actual completion."""
        return None

    def authorize_request(self,request,uow):
        work=self._sending
        if self.closed or work is None or request.binding.role!='MEDIA' or not self.admitted() or self.runtime.gate.information_checkpoint() is None:return False
        if request.material is not self._sending_input:return False
        description=request.description
        if (work['phase']!='READY_TO_REQUEST' or work['original_operation_key']!=description['original_request_key'] or work['work_id']!=description['work_id']
                or work['deadline_at_us']!=description['deadline_at_us'] or time.time_ns()//1000>=cast(int,work['deadline_at_us'])):return False
        try:self.provider.verify_request_material(request,uow)
        except (InvalidValue,OwnerFailure):return False
        return True

    async def prepare(self,preparation_id:str):
        if not self.admitted():return Found(MappingProxyType({'state':'WAITING_ADMISSION','new_sends':0}))
        outcome=await super().prepare(preparation_id)
        if type(outcome) is Found and outcome.value.get('reason')=='INPUT_REJECTED':
            return await self.invalidate_input_preparation(preparation_id)
        return outcome

    async def drive(self,work,*,fresh:bool):
        if self.closed or self._task is not None:raise OwnerFailure('RESOURCE_BUSY','media','CLEANUP_PENDING',True)
        from companion_memory.persistence.completion import start_owned
        task,logical=start_owned(self._drive_daily(work,fresh));self._task=task;self.runtime.retain_external_work(task)
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(5,(cast(int,work['deadline_at_us'])-time.time_ns()//1000)/1000000))
        return logical.result() if done else Found(MappingProxyType({'state':'PENDING','cleanup_pending':True}))

    async def _drive_daily(self,work,fresh):
        wid=cast(str,work['work_id']);key=cast(str,work['original_operation_key']);provider=self.provider;owner=self.image_work
        if work['request_association_state']=='INPUT_REJECTED':
            if not fresh:return Found(MappingProxyType({'state':'WAITING_ADMISSION','reason':'INPUT_REJECTED','new_sends':0,'cleanup_pending':False}))
            return await self.invalidate_input_preparation(cast(str,work['admission_preparation_id']))
        if work['request_association_state']=='PROCESSING_RELEASED':return Found(MappingProxyType({'state':'RESULT_STORED','cleanup_pending':False}))
        original=await provider.read_daily_request(key,'MEDIA',wid,time.monotonic()+5)
        if type(original) is NotFound and not fresh:return Found(MappingProxyType({'state':'WAITING_ADMISSION','new_sends':0}))
        if type(original) is NotFound:
            deadline=time.monotonic()+max(0,(cast(int,work['deadline_at_us'])-time.time_ns()//1000)/1000000)
            if self.provider.network is None:raise InvalidValue()
            await self.provider.network.wait_quiet(deadline)
            lease=await self.images.acquire(wid,deadline)
            if type(lease) is ImageRejected:
                try:
                    await self.images.wait_actual()
                    rejected=await owner.execute('reject_daily_image_input',owner.key('image-input-reject',wid),{'work_id':wid,'expected_revision':work['revision'],'reason':lease.reason})
                    if type(rejected) is not Committed:return rejected
                finally:
                    await owner.wait_actual()
                    self.images.release_rejection()
                return await self.invalidate_input_preparation(cast(str,work['admission_preparation_id']))
            if type(lease) is not DailyImageLease:raise InvalidValue()
            request=None
            try:
                supplied=image_input(lease)
                request=provider.image_request(supplied,key);self._sending=work;self._sending_input=supplied
                sent=await provider.send_generation(request)
                original=await provider.read_daily_request(key,'MEDIA',wid,time.monotonic()+5)
                if type(original) is not Found:return sent
            finally:
                if request is not None:
                    await provider.wait_generation_actual(request);provider.release_unused_generation(request)
                self._sending=None;self._sending_input=None;await self.images.wait_actual();self.images.release(lease)
                await provider.reconcile_daily_network()
        if type(original) is not Found:raise InvalidValue()
        request=original.value;rid=cast(str,request['object_id']);digest=cast(str,request['fingerprint'])
        if work['provider_request_id'] is None:
            associated=await owner.execute('associate_daily_image',owner.key('image-associate',wid,rid),{'work_id':wid,'expected_revision':work['revision'],'request_id':rid,'request_digest':digest})
            if type(associated) is not Committed:return associated
            work=(await self.media.rows.read('work_get',{'work_id':wid}))[0]
        if request['phase'] not in ('TERMINAL','REMOTE_RESULT_UNKNOWN'):return Found(MappingProxyType({'state':'PENDING','cleanup_pending':provider.cleanup_pending}))
        if work['phase'] not in ('RESULT_STORED','REMOTE_UNKNOWN'):
            guard_key=None
            if request['outcome']=='SENSITIVE_REFUSAL':
                observed=await self._observed(cast(str,work['occurrence_id']),cast(str,work['authorization_domain_id']))
                if observed is None:raise OwnerFailure('STORAGE_FAILED','media','INTEGRITY_FAILURE')
                guard_key=observed[2]
                self.runtime.gate.close_guard(guard_key)
            received=await provider.recover_daily_result(rid,time.monotonic()+5) if request['outcome']=='SUCCEEDED' else None
            try:
                stored=await owner.receive(work,request,digest,received)
                if type(stored) is not Committed:return stored
                if guard_key is not None:self.runtime.gate.guard_committed(guard_key)
            finally:
                if received is not None:provider.release_daily_result(received)
            work=(await self.media.rows.read('work_get',{'work_id':wid}))[0]
        if work['phase']=='REMOTE_UNKNOWN':return Found(MappingProxyType({'state':'REMOTE_UNKNOWN','cleanup_pending':provider.cleanup_pending}))
        if request['outcome']=='SUCCEEDED':
            receipt=await owner.operations['store_daily_image'].read_receipt(owner.key('image-result',wid,rid))
            if type(receipt) is not Found:raise InvalidValue()
            cleanup=await provider.cleanup_daily(rid,receipt.value,time.monotonic()+5)
            if type(cleanup) is not Found:return cleanup
            if cleanup.value.get('cleanup_pending'):return Found(MappingProxyType({'state':'PENDING','cleanup_pending':True}))
        retired=await owner.retire(work,request,digest)
        if type(retired) is not Committed:return retired
        return Found(MappingProxyType({'state':'RESULT_STORED','cleanup_pending':False}))

    async def invalidate_input_preparation(self,preparation_id:str):
        """Dispose the actual unusable window, preserving its unconsumed input."""
        from .preparation_disposal import dispose_preparation
        outcome=await dispose_preparation(self.runtime,preparation_id)
        if type(outcome) is not Committed:return outcome
        return Found(MappingProxyType({'state':'INVALIDATED','reason':'INPUT_REJECTED','new_sends':0,'cleanup_pending':False}))

    def close(self):
        self.closed=True
        return self._task is None and self._sending is None and self.images.close()
