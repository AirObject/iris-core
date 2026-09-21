"""Finite native mixed-Provider commands with actual accounting audit facts.

Inputs carry original references only. Registration and completion material are
retained by the single Provider owner, never supplied as a caller's mutation.
"""
from collections.abc import Callable
from companion_memory.persistence import ResultBoundCommandDefinition,RepositoryDefinition,UnitOfWork,Field,RecordSchema,AuditFieldBinding,AuditResultBinding
from companion_memory.persistence.semantic_records import ID,H,P,N,REQUEST,RECEIPT,Record,record,enum
from companion_memory.persistence.record_primitives import TARGETS
from companion_memory.logging_service import AuditRequirement
from .ledger import CHANGE

FACT=RecordSchema(CHANGE.fields+(Field('billing_mode',enum('TOKEN_METERED','USAGE_ONLY_TRIAL')),Field('currency',enum('CNY')),
    Field('quota_known',N,nullable=True),Field('quota_held',N),Field('config_snapshot_id',ID)))

class DailyProviderCommands:
    """Exact bounded original-key operations; none is an external-send endpoint."""
    def __init__(self,provider:RepositoryDefinition,readers:tuple[RepositoryDefinition,...], *, dream_format: bool = False):
        if provider.owner_module!='provider' or provider.schema_version!=(6 if dream_format else 5):raise ValueError('Native daily Provider repository required.')
        self.handler:Callable[[str,UnitOfWork,Record],object]|None=None
        layouts={
            'register_daily_request':record(operation_id=ID,request_ref=REQUEST,original_request_digest=H),
            'store_daily_handoff':record(operation_id=ID,request_ref=REQUEST,original_request_digest=H,terminal_evidence_ref=RECEIPT),
            'confirm_daily_handoff':record(operation_id=ID,request_ref=REQUEST,receipt=RECEIPT),
            'retire_daily_handoff':record(operation_id=ID,request_ref=REQUEST,expected_handoff_revision=P,after_ordinal=(N,))}
        definitions=[]
        for kind,schema in layouts.items():
            audit=AuditRequirement('provider','provider_change',kind.upper(),1,('APPLY',),FACT,target_limit=16)
            bindings=(AuditResultBinding('provider_change',1,(AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),
                AuditFieldBinding('actor_ref','INTENT',('actor',)),AuditFieldBinding('reason_code','CONSTANT',constant='APPLY'),
                AuditFieldBinding('target_refs','RESULT',('targets',)),AuditFieldBinding('change','RESULT',('fact',)))),)
            def handle(uow:UnitOfWork,v:Record,action=kind):
                if self.handler is None:raise ValueError('Native daily Provider owner is not bound.')
                return self.handler(action,uow,v)
            definitions.append(ResultBoundCommandDefinition('provider',kind,6 if dream_format else 5,schema,1,
                record(operation_id=ID,state=enum('REGISTERED','STORED','RECEIVED','RETIRING','RETIRED'),targets=TARGETS,fact=FACT),
                (provider,)+readers,(audit,),handle,record(actor=ID),bindings))
        self.commands=tuple(definitions)
