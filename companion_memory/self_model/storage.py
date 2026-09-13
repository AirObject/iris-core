"""Native owner of immutable persona sources and versioned review records.

All writes participate in the caller's existing transaction. They neither issue
Provider requests nor grant management authority. Coordination must verify the
original Provider completion and runtime mode before invoking a write effect.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.text_persistence import StoredTextConfiguration,stored_text_configuration_issue
from companion_memory.persistence import PersistenceService,UnitOfWork,Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import BoundStatements,StatementCatalog,OwnerFailure
from companion_memory.persistence.text_records import decode_row,stable_identity
from companion_memory.persistence.schema import InvalidValue
from .formats import RUN,CANDIDATE,PUBLICATION,isolate_run,isolate_candidate,isolate_publication,projection,candidate_digest
from .transitions import ReviewedPersona,matching_generation


@dataclass(frozen=True,slots=True)
class PersonaRecord:
    """One complete owner record; absence is represented separately by None."""
    value: MappingProxyType[str,Value]


class PersonaStorage:
    """One configuration-bound self-model lease and fixed point-read catalog."""
    def __init__(self,catalog: StatementCatalog,storage: PersistenceService,configuration: StoredTextConfiguration,instance_id: str):
        if (stored_text_configuration_issue(configuration) is not None or catalog.definition.owner_module!='self_model'
                or catalog.definition.schema_version!=1):raise InvalidValue()
        self._storage=storage;self._configuration=configuration;self._instance=instance_id
        self._rows=BoundStatements(catalog,storage,instance_id)
        self._lease=storage.claim_module_owner(catalog.definition)
        if self._lease is None:raise InvalidValue()
        self._closed=False

    def _decode(self,kind: str,row):
        schema,validate={'run':(RUN,isolate_run),'candidate':(CANDIDATE,isolate_candidate),'publication':(PUBLICATION,isolate_publication)}[kind]
        value=validate(decode_row(row,schema,4096,self._configuration.database_id,self._instance,self._configuration.snapshot_id))
        expected=stable_identity('persona-run' if kind=='run' else 'persona-publication',self._configuration.database_id,self._instance) if kind!='candidate' else stable_identity(
            'persona-candidate',self._configuration.database_id,self._instance,value['run_id'],value['generation'])
        if value['object_id']!=expected:raise InvalidValue()
        return value

    @staticmethod
    def _table(kind: str) -> str:
        return {'run':'initial_persona_runs','candidate':'initial_persona_candidates','publication':'persona_publications'}[kind]

    def read(self,uow: UnitOfWork,kind: str,object_id: str) -> PersonaRecord | None:
        """Read one retained identity inside an already authorized transaction."""
        if self._closed:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        rows=self._rows.stage(self._table(kind)+'_get',uow,{'object_id':object_id})
        if len(rows)>1:raise InvalidValue()
        return PersonaRecord(self._decode(kind,rows[0])) if rows else None

    async def read_original(self,kind: str,object_id: str,deadline: float) -> PersonaRecord | None:
        """Use the caller's original absolute deadline without broad history access."""
        from companion_memory.persistence.deadlines import DeadlineScope
        import time
        if self._closed:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        with DeadlineScope(deadline):
            if time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
            rows=await self._rows.read(self._table(kind)+'_get',{'object_id':object_id})
            if time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
        if len(rows)>1:raise InvalidValue()
        return PersonaRecord(self._decode(kind,rows[0])) if rows else None

    def _insert(self,uow: UnitOfWork,kind: str,value: MappingProxyType[str,Value]) -> None:
        if self._closed:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        uow.require_commit_permission(lambda:not self._closed)
        raw={'object_id':value['object_id'],'revision':value['revision'],'body':encode_content(value,4096).decode()}
        if value['revision']!=1 or self._decode(kind,raw)!=value:raise InvalidValue()
        rows=self._rows.stage(self._table(kind)+'_insert',uow,raw)
        if len(rows)!=1 or self._decode(kind,rows[0])!=value:raise InvalidValue()

    def _replace(self,uow: UnitOfWork,kind: str,previous: MappingProxyType[str,Value],value: MappingProxyType[str,Value]) -> None:
        if self._closed or kind=='publication':raise InvalidValue()
        uow.require_commit_permission(lambda:not self._closed)
        actual=self.read(uow,kind,cast(str,previous['object_id']))
        if actual is None or actual.value!=previous:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        immutable=('object_id','database_id','instance_id','config_snapshot_id','created_at_us')
        if any(previous[name]!=value[name] for name in immutable) or value['revision']!=cast(int,previous['revision'])+1:raise InvalidValue()
        if kind=='candidate' and candidate_digest(previous)!=candidate_digest(value):raise InvalidValue()
        raw={'object_id':value['object_id'],'revision':value['revision'],'body':encode_content(value,4096).decode()}
        if self._decode(kind,raw)!=value:raise InvalidValue()
        changed=self._rows.stage(self._table(kind)+'_cas',uow,{**raw,'expected_revision':previous['revision']})
        if len(changed)!=1 or self._decode(kind,changed[0])!=value:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')

    def stage_run(self,uow: UnitOfWork,value: object) -> MappingProxyType[str,Value]:
        """Insert the sole run together with runtime's actual mode transition."""
        run=isolate_run(value)
        if run['state']!='PREPARED' or run['generation']!=1:raise InvalidValue()
        self._insert(uow,'run',run)
        return run

    def update_run(self,uow: UnitOfWork,previous: object,value: object) -> MappingProxyType[str,Value]:
        """CAS the complete current run; transition authority remains with coordination."""
        before=isolate_run(previous);after=isolate_run(value)
        self._replace(uow,'run',before,after)
        return after

    def stage_resolution(self,uow: UnitOfWork,previous: object,run: object,candidate: object) -> None:
        """Publish one complete generation resolution and its run pointer together."""
        before=isolate_run(previous);after=isolate_run(run);resolved=isolate_candidate(candidate)
        matching_generation(after,resolved)
        if (before['state'] not in ('REQUEST_ASSOCIATED','REMOTE_UNKNOWN') or after['state'] not in ('WAITING_REVIEW','KNOWN_FAILED')
                or resolved['review'] not in ('PENDING','NOT_APPLICABLE') or before['generation']!=after['generation']):raise InvalidValue()
        self._insert(uow,'candidate',resolved)
        self._replace(uow,'run',before,after)

    def stage_review(self,uow: UnitOfWork,previous_run: object,previous_candidate: object,reviewed: ReviewedPersona) -> None:
        """Apply both review versions; a failure in either owner write rolls back both."""
        if type(reviewed) is not ReviewedPersona:raise InvalidValue()
        before_run=isolate_run(previous_run);before_candidate=isolate_candidate(previous_candidate)
        matching_generation(reviewed.run,reviewed.candidate)
        if (before_run['state']!='WAITING_REVIEW' or before_candidate['review']!='PENDING'
                or reviewed.run['state'] not in ('APPROVED','USER_REJECTED')):raise InvalidValue()
        self._replace(uow,'candidate',before_candidate,reviewed.candidate)
        self._replace(uow,'run',before_run,reviewed.run)

    def stage_publication(self,uow: UnitOfWork,previous_run: object,run: object,publication: object) -> None:
        """Insert an approved immutable publication with its terminal run version."""
        before=isolate_run(previous_run);after=isolate_run(run);published=isolate_publication(publication)
        found=self.read(uow,'candidate',cast(str,published['candidate_id']))
        if found is None:raise InvalidValue()
        candidate=found.value;matching_generation(before,candidate)
        if (before['state']!='APPROVED' or candidate['review']!='APPROVED' or after['state']!='PUBLISHED'
                or published['candidate_revision']!=candidate['revision'] or published['candidate_digest']!=candidate_digest(candidate)
                or after['publication_id']!=published['object_id'] or published['run_id']!=before['object_id']):raise InvalidValue()
        if any(published[name]!=candidate[name] for name in ('generation','input_id','input_digest','provider_request_id','handoff_id','text','reviewed_by','review_operation')):raise InvalidValue()
        if any(published[name]!=before[name] for name in ('self_subject_id','self_revision','prompt_ref','schema_ref','transform_ref')):raise InvalidValue()
        self._validate_published(after,candidate,published)
        self._insert(uow,'publication',published)
        self._replace(uow,'run',before,after)

    @staticmethod
    def _validate_published(run: MappingProxyType[str,Value],candidate: MappingProxyType[str,Value],publication: MappingProxyType[str,Value]) -> None:
        matching_generation(run,candidate)
        if (run['state']!='PUBLISHED' or candidate['review']!='APPROVED' or candidate['resolution']!='SUCCEEDED'
                or publication['revision']!=1 or run['publication_id']!=publication['object_id'] or publication['run_id']!=run['object_id']
                or publication['candidate_id']!=candidate['object_id'] or publication['candidate_revision']!=candidate['revision']
                or publication['candidate_digest']!=candidate_digest(candidate) or run['last_operation']!=publication['publication_operation']
                or cast(int,publication['generated_at_us'])>cast(int,publication['created_at_us'])
                or cast(int,candidate['reviewed_at_us'])>cast(int,publication['created_at_us'])
                or cast(int,run['updated_at_us'])<cast(int,publication['created_at_us'])):raise InvalidValue()
        if any(publication[name]!=candidate[name] for name in ('database_id','instance_id','config_snapshot_id','generation','input_id','input_digest',
                'provider_request_id','handoff_id','text','reviewed_by','review_operation')):raise InvalidValue()
        if any(publication[name]!=run[name] for name in ('self_subject_id','self_revision','prompt_ref','schema_ref','transform_ref')):raise InvalidValue()

    def current_publication(self,uow: UnitOfWork) -> PersonaRecord | None:
        """Read the unique current publication and all of its retained review roots."""
        identity=stable_identity('persona-publication',self._configuration.database_id,self._instance)
        found=self.read(uow,'publication',identity)
        if found is None:return None
        run=self.read(uow,'run',cast(str,found.value['run_id']))
        candidate=self.read(uow,'candidate',cast(str,found.value['candidate_id']))
        if run is None or candidate is None:raise InvalidValue()
        self._validate_published(run.value,candidate.value,found.value)
        return found

    async def current_original(self,deadline: float) -> PersonaRecord | None:
        """Read the current immutable publication under one inherited deadline.

        This owner method grants no public read or mode exemption. Its consumer
        must retain the original read capability through final response delivery.
        """
        identity=stable_identity('persona-publication',self._configuration.database_id,self._instance)
        found=await self.read_original('publication',identity,deadline)
        if found is None:return None
        run=await self.read_original('run',cast(str,found.value['run_id']),deadline)
        candidate=await self.read_original('candidate',cast(str,found.value['candidate_id']),deadline)
        if run is None or candidate is None:raise InvalidValue()
        self._validate_published(run.value,candidate.value,found.value)
        if self._closed:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        return found

    def current_projection(self,uow: UnitOfWork,*,stale: bool = False):
        """Read the sole published persona within a context-freezing transaction."""
        found=self.current_publication(uow)
        return projection(found.value,stale) if found else None

    def close(self) -> bool:
        """Close admission while actual outstanding storage jobs retain their lease."""
        self._closed=True
        assert self._lease is not None
        return self._lease.release()
