"""Memory-owned point records reconstruct a source through its bounded leaf ports."""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import UnitOfWork, Value
from companion_memory.persistence.owned_statements import BoundStatements, OwnerFailure
from companion_memory.persistence.schema import InvalidValue
from .source_records import split_manifest, member_digests, join_manifest


class MemorySourceRows(BoundStatements):
    """Only source headers use a composed read; every stored record stays bounded."""
    def __init__(self,catalog,storage,scope,*,semantic_format: bool=False):
        super().__init__(catalog,storage,scope)
        self.semantic_format=semantic_format

    def stage(self, name: str, uow: UnitOfWork, parameters: object) -> tuple[MappingProxyType[str, Value], ...]:
        try:
            logical = None
            if name in ('sources_insert', 'sources_references'):
                values = cast(dict, parameters)
                if values['body'] is not None:
                    logical = values['body']; header, _ = split_manifest(logical,semantic_format=self.semantic_format)
                    parameters = {**values, 'body': header}
            rows = super().stage(name, uow, parameters)
            if logical is not None:
                return tuple(MappingProxyType({**row, 'body': logical}) for row in rows)
            if name != 'sources_get' or not rows or rows[0]['body'] is None: return rows
            row = rows[0]; body = cast(str, row['body']); members = []
            for ordinal in range(len(member_digests(body,semantic_format=self.semantic_format))):
                leaf = super().stage('source_members_get', uow, {'source_id': row['source_id'], 'ordinal': ordinal})
                if not leaf: raise InvalidValue()
                members.append(cast(str, leaf[0]['body']))
            return (MappingProxyType({**row, 'body': join_manifest(body, tuple(members),semantic_format=self.semantic_format)}),)
        except InvalidValue as failure:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE') from failure

    async def read(self, name: str, parameters: object) -> tuple[MappingProxyType[str, Value], ...]:
        try:
            rows = await super().read(name, parameters)
            if name not in ('sources_get', 'review_manifest') or not rows or rows[0]['body'] is None: return rows
            row = rows[0]; body = cast(str, row['body']); members = []
            for ordinal in range(len(member_digests(body,semantic_format=self.semantic_format))):
                if name == 'review_manifest':
                    leaf = await super().read('review_member', {**cast(dict, parameters), 'ordinal': ordinal})
                else: leaf = await super().read('source_members_get', {'source_id': row['source_id'], 'ordinal': ordinal})
                if not leaf: raise OwnerFailure('PRECONDITION_FAILED', 'source', 'SOURCE_CHANGED')
                members.append(cast(str, leaf[0]['body']))
            if await super().read(name, parameters) != rows:
                raise OwnerFailure('PRECONDITION_FAILED', 'source', 'SOURCE_CHANGED')
            return (MappingProxyType({**row, 'body': join_manifest(body, tuple(members),semantic_format=self.semantic_format)}),)
        except InvalidValue as failure:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE') from failure
