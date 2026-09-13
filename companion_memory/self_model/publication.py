"""Reconstitute approved persona text and provenance before atomic publication.

No model call, user decision or mode transition occurs here. The owning command
must verify the actual SELF and mode epoch, then stage the returned publication,
run and runtime FINISH together with their required audits and original receipt.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import cast

from companion_memory.configuration.text_persistence import StoredTextConfiguration
from companion_memory.persistence.schema import InvalidValue, Value, freeze_value
from companion_memory.persistence.text_records import UINT, stable_identity
from companion_memory.provider.completion_evidence import ConfirmedCompletion
from companion_memory.provider.values import as_record
from .formats import isolate_run, isolate_candidate, isolate_publication, candidate_digest,projection,query_projection
from .resolution import verify_resolved
from .transitions import _operation, matching_generation


@dataclass(frozen=True, slots=True)
class PublishedPersona:
    """Immutable publication and the matching terminal run CAS proposal."""
    run: MappingProxyType[str, Value]
    publication: MappingProxyType[str, Value]


def publish_approved(configuration: StoredTextConfiguration, initial: object, run: object, candidate: object,
                     expected_revision: int, candidate_revision: int, expected_digest: str,
                     completion: ConfirmedCompletion, operation: object, now_us: int) -> PublishedPersona:
    """Match the approved candidate to its original native result before publishing.

    A reconstructed resolution is only a comparison value. It never replaces a
    stored run state or authorizes a new resolution, review or Provider request.
    """
    current=isolate_run(run); reviewed=isolate_candidate(candidate)
    freeze_value(UINT,now_us)
    matching_generation(current,reviewed)
    if (current['state']!='APPROVED' or current['revision']!=expected_revision
            or reviewed['revision']!=candidate_revision or reviewed['review']!='APPROVED'
            or reviewed['resolution']!='SUCCEEDED' or candidate_digest(reviewed)!=expected_digest
            or now_us<cast(int,current['updated_at_us']) or now_us<cast(int,reviewed['reviewed_at_us'])):
        raise InvalidValue()
    key=_operation(operation,'publish_initial_persona',current['instance_id'])
    verify_resolved(configuration,initial,current,reviewed,completion,now_us)
    result=as_record(completion.terminal.result)
    # Preserve the confirmed local terminal observation, not publication time
    # or an unverified supplier timestamp. Integer arithmetic retains microseconds.
    completed=datetime.fromisoformat(cast(str,completion.terminal.request['updated_at']))-datetime(1970,1,1,tzinfo=timezone.utc)
    generated_at_us=(completed.days*86400+completed.seconds)*1000000+completed.microseconds
    if not 0<=generated_at_us<=now_us:raise InvalidValue()
    identity=stable_identity('persona-publication',configuration.database_id,cast(str,current['instance_id']))
    published=isolate_publication({'format_version':1,'object_id':identity,'revision':1,
        'database_id':configuration.database_id,'instance_id':current['instance_id'],'config_snapshot_id':configuration.snapshot_id,
        'created_at_us':now_us,'run_id':current['object_id'],'generation':current['generation'],
        'candidate_id':reviewed['object_id'],'candidate_revision':reviewed['revision'],'candidate_digest':expected_digest,
        **{name:reviewed[name] for name in ('input_id','input_digest','provider_request_id','handoff_id','text','reviewed_by','review_operation')},
        **{name:current[name] for name in ('self_subject_id','self_revision','prompt_ref','schema_ref','transform_ref')},
        **{name:result[name] for name in ('requested_model_id','reported_model_id','resolved_model_id')},
        'generated_at_us':generated_at_us,'publication_operation':key})
    # Both states must fit before publication; a later stale flag cannot turn
    # this immutable approved text into a truncated or unavailable section.
    for stale in (False,True):query_projection(projection(published,stale))
    updated=isolate_run({**current,'revision':expected_revision+1,'state':'PUBLISHED','publication_id':identity,
        'last_operation':key,'updated_at_us':now_us})
    return PublishedPersona(updated,published)
