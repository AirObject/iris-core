"""Cognition-owned frozen reviewed set and member storage declarations.

Stored/established counts belong to the set owner. Body codecs do not grant
review authority or permit creating memory through these storage declarations.
"""
from companion_memory.persistence.semantic_catalog import SemanticTable,catalog,projection
from .fixed_memory_schema import FIXED_SET,FIXED_MEMBER,validate_set,validate_member

TABLES=(
    SemanticTable('fixed_memory_set',(FIXED_SET,),validate_set,(projection('set_id'),),('UNIQUE(scope_id,set_id)',)),
    SemanticTable('fixed_memory_member',(FIXED_MEMBER,),validate_member,
        (projection('set_id'),projection('ordinal',numeric=True),projection('member_id')),
        ('UNIQUE(scope_id,set_id,ordinal)','UNIQUE(scope_id,set_id,member_id)','CHECK(ordinal BETWEEN 0 AND 4095)',
         'FOREIGN KEY(scope_id,set_id) REFERENCES cognition_fixed_memory_set(scope_id,set_id)')),
)


def fixed_memory_catalog():
    return catalog('cognition',TABLES)
