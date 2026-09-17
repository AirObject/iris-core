"""Native focused run entry and safe exit reuse the content FIFO drain owner."""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from companion_memory.self_model.periodic_persona import PeriodicPersona
from dataclasses import replace
from typing import cast
from companion_memory.persistence import ResultBoundCommandDefinition,RecordSchema,UnitOfWork
from companion_memory.persistence.schema import BoundedTextSchema,InvalidValue
from companion_memory.persistence.semantic_records import Record,ID,P,N,fields,enum
from companion_memory.persistence.daily_records import identity
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.dream.control import DreamControl
from companion_memory.dream.records import RUN_STATES,validate_run
from companion_memory.dream.results import audits,result_schema,result,target,INTENT


class DreamMode:
    def __init__(self,content,control:DreamControl,previous,participants:tuple):
        self.content=content;self.control=control;self.previous=previous;self.runtime=None;self.persona:PeriodicPersona|None=None
        control.focused_ready=self.focused_ready;control.run_admission=self.admit
        self.definition=replace(previous.definition,command_version=4,handler=self.handle_mode)
        definitions=[]
        common=fields(run_id=ID,expected_revision=P,mode_epoch=N)
        for kind,owners,parameters in (
            ('start_focused_dream',('dream','runtime'),common+fields(trigger=enum('MANUAL','SCHEDULED'),local_date=(BoundedTextSchema(10),))),
            ('exit_focused_dream',('dream','runtime'),common),('finish_focused_dream',('dream','runtime'),common),
            ('finish_background_dream',('dream',),common),('complete_dream_exit',('dream',),common)):
            required,bindings=audits(kind,owners)
            def handle(uow,values,operation=kind):
                try:return self.handle(operation,uow,values)
                except OwnerFailure as failure:
                    control.causes.record(operation,values,failure)
                    raise
            definitions.append(ResultBoundCommandDefinition('dream',kind,1,RecordSchema(fields(operation_id=ID)+parameters),1,
                result_schema(owners,RUN_STATES),participants,required,handle,INTENT,bindings))
        self.commands=tuple(definitions)

    def bind(self,runtime):
        if self.runtime is not None or runtime.assembly is not self.content:raise InvalidValue()
        self.runtime=runtime

    def current(self,uow):
        rows=self.content.rows.stage('mode_get',uow,{'mode_id':'instance_mode'})
        if len(rows)!=1:raise InvalidValue()
        return rows[0]

    def focused_ready(self,uow:UnitOfWork,run:Record)->int:
        mode=self.current(uow)
        if mode['state']!='DREAM_FOCUSED' or mode['run_id']!=run['run_id'] or mode['epoch'] not in (run['mode_epoch'],cast(int,run['mode_epoch'])+1):
            raise OwnerFailure('PRECONDITION_FAILED','mode','REVISION_CONFLICT')
        return cast(int,mode['epoch'])

    def admit(self,uow:UnitOfWork,run:Record):
        mode=self.current(uow)
        if run['mode']=='FOCUSED':
            if mode['state']!='DREAM_FOCUSED' or mode['run_id']!=run['run_id'] or mode['epoch']!=run['mode_epoch']:
                raise OwnerFailure('MODE_BLOCKED','state','NOT_READY')
        elif mode['state'] not in ('NORMAL','DRAINING') or mode['epoch']!=run['mode_epoch']:
            raise OwnerFailure('MODE_BLOCKED','state','NOT_READY')

    def handle(self,kind,uow,v):
        if self.runtime is None:raise InvalidValue()
        control=self.control;now=control._ready(uow);mode=self.current(uow);op=control._operation(uow)
        root=control.rows.get('schedule',uow,control.root_id)
        if root is None:raise InvalidValue()
        facts:dict[str,object]={}
        if kind=='start_focused_dream':
            if mode['state']!='NORMAL' or mode['epoch']!=v['mode_epoch']:raise OwnerFailure('PRECONDITION_FAILED','mode','REVISION_CONFLICT')
            epoch=cast(int,mode['epoch'])+1
            run,targets=control.start_run(uow,v,now,op,root,mode='FOCUSED',state='PREPARING',epoch=epoch)
            self.content.rows.stage('mode_update',uow,dict(mode)|{'state':'DREAM_PREPARING','epoch':epoch,'run_id':run['run_id'],
                'publication_id':None,'deadline_at_us':now+self.content.configuration.candidate.runtime.integer('runtime.focus_drain_timeout_ms')*1000})
            state='PREPARING'
        else:
            run=control.participate_run(uow,v['run_id'],v['expected_revision'],v['mode_epoch'])
            if root['active_run_id']!=run['run_id'] or not control.settled(run):raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')
            previous=cast(int,run['revision']);targets=[target(cast(str,run['run_id']),previous+1,previous)]
            if kind in ('exit_focused_dream','finish_focused_dream','finish_background_dream'):
                focused=kind!='finish_background_dream'
                if run['mode']!=('FOCUSED' if focused else 'BACKGROUND') or run['state'] not in ('RUNNING','PAUSED','FINALIZING'):
                    raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')
                if focused:self.focused_ready(uow,run)
                self.runtime.provider.require_dream_exit(uow)
                if control.exit_readiness is not None:control.exit_readiness(uow,run)
                outcome='ABORTED_SAFELY';publication_id=None;publication_revision=None
                if kind!='exit_focused_dream':
                    if self.persona is None:raise InvalidValue()
                    outcome,publication_id,publication_revision=self.persona.exit_evidence(uow,run)
                eid=identity('dream-exit',control.configuration.database_id,control.configuration.scope_id,run['run_id'])
                control.rows.write('exits',uow,control._base(eid,now)|{'run_id':run['run_id'],'mode_epoch':mode['epoch'],
                    'outcome':outcome,'publication_id':publication_id,'publication_revision':publication_revision,'original_operation':op})
                targets.append(target(eid,1));state='DRAINING' if focused else 'COMPLETED';epoch=cast(int,mode['epoch'])+1 if focused else cast(int,mode['epoch'])
                if focused:self.content.rows.stage('mode_update',uow,dict(mode)|{'state':state,'epoch':epoch})
                else:
                    rootrev=cast(int,root['revision'])
                    control.rows.write('schedule',uow,dict(root)|{'revision':rootrev+1,'updated_at_us':max(now,cast(int,root['updated_at_us'])),'active_run_id':None,**{k:run[k] for k in ('self_cursor','object_cursor','expiry_after_us','expiry_after_id','impact_cursor')},'last_operation':op},rootrev)
                    targets.append(target(control.root_id,rootrev+1,rootrev))
                changed=dict(run)|{'exit_result':outcome,'end_reason':'ABORTED_SAFELY' if outcome=='ABORTED_SAFELY' else 'PERSONA_UNCHANGED' if outcome=='NO_PERSONA_CHANGE' else run['end_reason'],'mode_epoch':epoch}

            else:
                if run['state']!='DRAINING' or mode['state']!='NORMAL' or mode['run_id']!=run['run_id'] or mode['epoch']!=cast(int,run['mode_epoch'])+1:
                    raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')
                state='ABORTED' if run['exit_result']=='ABORTED_SAFELY' else 'COMPLETED'
                changed=dict(run)|{'mode_epoch':mode['epoch']}
                rootrev=cast(int,root['revision'])
                control.rows.write('schedule',uow,dict(root)|{'revision':rootrev+1,'updated_at_us':max(now,cast(int,root['updated_at_us'])),
                    'active_run_id':None,**{k:run[k] for k in ('self_cursor','object_cursor','expiry_after_us','expiry_after_id','impact_cursor')},'last_operation':op},rootrev)
                targets.append(target(control.root_id,rootrev+1,rootrev))
            control.rows.write('runs',uow,validate_run(changed|{'state':state,'revision':previous+1,
                'updated_at_us':max(now,cast(int,run['updated_at_us'])),'last_operation':op}),previous)
        counts=control.storage.transaction_row_changes(uow)
        facts['dream']={'rows_changed':counts['dream'],'targets':targets}
        if kind not in ('complete_dream_exit','finish_background_dream'):
            runtime_targets = [target('instance_mode', cast(int, mode['epoch']) + 1, cast(int, mode['epoch']))]
            if kind == 'start_focused_dream' and control.work_configuration is not None:
                runtime_targets.append(target(control.work_configuration.key('DREAM', v['run_id']), 1))
            facts['runtime'] = {'rows_changed': counts['runtime'], 'targets': runtime_targets}
        return result(v['operation_id'],state,facts)

    def handle_mode(self,uow,v):
        control=self.control
        run=control.rows.get('runs',uow,v['run_id']) if control.bound else None
        if run is None:return self.previous.handle(uow,v)
        mode=self.current(uow)
        if v['action'] not in ('READY','DRAINED','FAULT') or run['mode']!='FOCUSED' or mode['run_id']!=run['run_id']:
            raise OwnerFailure('ACCESS_DENIED','mode','OPERATION_NOT_GRANTED')
        if v['action']=='READY' and run['state'] not in ('PREPARING','PAUSED'):raise InvalidValue()
        if v['action']=='DRAINED':
            eid=identity('dream-exit',control.configuration.database_id,control.configuration.scope_id,run['run_id'])
            exit=control.rows.get('exits',uow,eid)
            if run['state']!='DRAINING' or exit is None or exit['outcome']!=run['exit_result']:raise InvalidValue()
        return self.content.modes.handle('change_content_mode',uow,v)

    async def synchronize(self):
        if self.runtime is None:raise InvalidValue()
        rows=await self.content.rows.read('mode_get',{'mode_id':'instance_mode'})
        if len(rows)!=1:raise InvalidValue()
        mode=rows[0];self.runtime.gate.resolve_cutoff(mode['state'],mode['epoch'])
        if mode['state']=='DREAM_PREPARING':await self.runtime.focus.drain(mode['run_id'])
        if mode['state']=='DRAINING':await self.runtime.focus.transfer_staged(mode['run_id'])
        rows=await self.content.rows.read('mode_get',{'mode_id':'instance_mode'})
        if len(rows)!=1:raise InvalidValue()
        self.runtime.gate.resolve_cutoff(rows[0]['state'],rows[0]['epoch'])
        return rows[0]
