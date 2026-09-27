"""Managed product accounts and profiles with explicit unpriced usage accounting.

The historical trial declarations remain byte-for-byte independent. Ordinary
usage accounting retains unknown supplier charges and permits a null cumulative
attempt limit; per-request capacity and uncertain-outcome isolation still apply.
"""
from dataclasses import replace
from companion_memory.persistence.schema import RecordSchema, ScalarSchema
from companion_memory.persistence.semantic_records import enum, integer
from .daily_schema import ACCOUNT as LEGACY_ACCOUNT, IMAGE_PROFILE as LEGACY_IMAGE, EMBEDDING_PROFILE as LEGACY_EMBEDDING
from .dream_schema import GENERATION_PROFILE as LEGACY_GENERATION

USAGE_ONLY_MODES = ('USAGE_ONLY_TRIAL', 'USAGE_ONLY')
ACCOUNT = RecordSchema(tuple(
    replace(field, schema=enum('TOKEN_METERED', *USAGE_ONLY_MODES)) if field.name == 'billing_mode'
    else replace(field, schema=integer(1, 1000000), nullable=True) if field.name == 'attempt_limit'
    else field for field in LEGACY_ACCOUNT.fields))

def profile(schema: RecordSchema) -> RecordSchema:
    """Add the ordinary observation mode without changing a legacy declaration."""
    return RecordSchema(tuple(replace(field, schema=enum(*field.schema.choices, 'USAGE_ONLY'))
        if field.name == 'billing_mode' and type(field.schema) is ScalarSchema else field for field in schema.fields))

GENERATION_PROFILE = profile(LEGACY_GENERATION)
IMAGE_PROFILE = profile(LEGACY_IMAGE)
EMBEDDING_PROFILE = profile(LEGACY_EMBEDDING)
