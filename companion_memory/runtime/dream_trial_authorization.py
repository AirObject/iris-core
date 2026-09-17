"""Closed dream trial purposes reuse native Provider registration and accounting.

This trusted activation is separate from configuration and grants no automatic
resume. Previous packages and failed work never contribute reusable slots.
"""
from hashlib import sha256
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.semantic_records import record,enum,ID,H,T,CONFIG,isolate
from companion_memory.persistence.schema import InvalidValue
from .daily_trial_authorization import DailyTrialAuthority,DailyTrialActivation

LIMITS={'DREAM_REVIEW':6,'PERSONA_DREAM':4,'PERSONA_REVIEW':4,'LEARNING':2,'GOAL_DEDUP':2,
    'EMBEDDING_DOCUMENT':12,'EMBEDDING_QUERY':2}
BINDING=record(format=enum('DREAM_TRIAL_AUTH_V1'),package_id=ID,config=CONFIG,code_digest=H,configuration_digest=H,
    resources_digest=H,materials_digest=H,decision_ref=ID,account_evidence_digest=H,input_evidence_digest=H,
    execution=enum('REAL','CONTROLLED'),expires_at=T)
REQUEST=record(slot_id=ID,role=enum(*LIMITS),request_id=ID,attempt_id=ID,operation_key=ID,work_id=ID,
    request_digest=H,wire_digest=H,account_id=ID,profile_id=ID)
ENTRY=record(request=REQUEST,state=enum('RESERVED','REGISTERED'),commit_id=(ID,),previous=H)


def purpose_slots():
    return tuple({'slot_id':role.lower()+':'+str(index),'role':role} for role,count in LIMITS.items() for index in range(count))


class DreamTrialAuthority(DailyTrialAuthority):
    """Only reviewed frozen work may select an unconsumed slot of its own role."""
    def activate(self,binding:object) -> DailyTrialActivation:
        value=isolate(BINDING,binding)
        if self.grant is not None or not self.verify(value):raise InvalidValue()
        grant=object.__new__(DailyTrialActivation)
        for key,item in (('authority',self),('binding',value),('digest',sha256(encode_content(value,8192)).hexdigest())):object.__setattr__(grant,key,item)
        self.grant=grant
        return grant
