"""Native initial-persona mode effects for the complete daily owner graph.

Preparing and publishing join actual self-model commands. The public generic
mode command can drain or recover those facts, but cannot enter or publish a
persona independently. No mode transition itself sends a Provider request.
"""
from dataclasses import replace
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure

class DailyPersonaMode:
    def __init__(self,content,participants):
        if not content.daily_format:raise InvalidValue()
        self.content=content;self.persona=None;self.runtime=None;self.closed=False
        previous=content.command_definition('change_content_mode')
        self.definition=replace(previous,command_version=3,participants=participants,handler=self.handle)

    def bind(self,persona,runtime):
        if self.persona is not None or runtime.assembly is not self.content:raise InvalidValue()
        self.persona=persona;self.runtime=runtime

    def current(self,uow):
        rows=self.content.rows.stage('mode_get',uow,{'mode_id':'instance_mode'})
        if len(rows)!=1:raise InvalidValue()
        return rows[0]

    def effect(self,uow,action,run_id,epoch,now,publication_id=None):
        if self.closed or self.persona is None or self.runtime is None:raise InvalidValue()
        mode=self.current(uow)
        expected='NORMAL' if action=='PREPARE' else 'DREAM_FOCUSED'
        if mode['state']!=expected or mode['epoch']!=epoch or mode['publication_id'] is not None or action!='PREPARE' and mode['run_id']!=run_id:
            raise OwnerFailure('PRECONDITION_FAILED','mode','REVISION_CONFLICT')
        values=dict(mode)|{'epoch':epoch+1}
        if action=='PREPARE':values.update(state='DREAM_PREPARING',run_id=run_id,deadline_at_us=now+self.content.configuration.candidate.runtime.integer('runtime.focus_drain_timeout_ms')*1000)
        elif action=='PUBLISH':values.update(state='DRAINING',publication_id=publication_id)
        elif action!='RETRY':raise InvalidValue()
        uow.require_commit_permission(lambda:not self.closed)
        self.content.rows.stage('mode_update',uow,values)
        return MappingProxyType(values)

    def handle(self,uow,values):
        if self.closed or self.persona is None or self.runtime is None:raise OwnerFailure('ACCESS_DENIED','mode','OPERATION_NOT_GRANTED')
        action=values['action']
        if action in ('ENTER','FINISH'):raise OwnerFailure('ACCESS_DENIED','mode','OPERATION_NOT_GRANTED')
        run=self.persona.participate_run(uow,values['run_id'])
        if action=='READY' and run['state']!='PREPARED':raise InvalidValue()
        if action=='DRAINED':
            publication=self.persona.participate_publication(uow)
            mode=self.current(uow)
            if run['state']!='PUBLISHED' or publication is None or publication['publication_id']!=run['publication_id'] or mode['publication_id']!=run['publication_id']:raise InvalidValue()
        return self.content.modes.handle('change_content_mode',uow,values)

    async def synchronize(self):
        """Publish only the current stored mode, never an older command's result."""
        if self.runtime is None:raise InvalidValue()
        rows=await self.content.rows.read('mode_get',{'mode_id':'instance_mode'})
        if len(rows)!=1:raise InvalidValue()
        mode=rows[0]
        self.runtime.gate.resolve_cutoff(cast(str,mode['state']),cast(int,mode['epoch']))
        if mode['state']=='DREAM_PREPARING':await self.runtime.focus.drain(mode['run_id'])
        if mode['state']=='DRAINING':await self.runtime.focus.transfer_staged(mode['run_id'])
        rows=await self.content.rows.read('mode_get',{'mode_id':'instance_mode'})
        if len(rows)!=1:raise InvalidValue()
        current=rows[0]
        self.runtime.gate.resolve_cutoff(cast(str,current['state']),cast(int,current['epoch']))
        return current

    def close(self):self.closed=True
