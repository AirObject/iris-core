"""Unpublished Persona drafts have an independent sequence and explicit closure."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PersonaDraft:
    id: str
    tenant_id: str
    agent_id: str
    revision: int
    status: str
    base_revision: int
    policy_revision: int
    fields_json: str | None
    source_refs_json: str | None
    content_hash: str
    created_by: str
    updated_by: str
    created_us: int
    updated_us: int
    published_revision_id: str | None
