"""Runtime owns separate complete member leaves for preparation and frozen batches."""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import UnitOfWork, Value
from companion_memory.persistence.owned_statements import BoundStatements, OwnerFailure
from companion_memory.persistence.schema import InvalidValue
from companion_memory.memory.source_records import split_manifest, member_digests, join_manifest


def member_key(kind: str, owner: str, ordinal: int) -> str:
    from .content_assembly import stable
    return stable('frozen_member', kind, owner, ordinal)


class RuntimeSourceRows(BoundStatements):
    """Compose only fixed owner statements; model and file work remain outside UoW."""
    def stage(self, name: str, uow: UnitOfWork, parameters: object) -> tuple[MappingProxyType[str, Value], ...]:
        try:
            kind = name.split('_', 1)[0]
            if kind not in ('preparations', 'batches'): return super().stage(name, uow, parameters)
            field = 'preparation_id' if kind == 'preparations' else 'batch_id'
            if name.endswith(('_insert', '_update')):
                values = cast(dict, parameters); logical = cast(str, values['manifest']); header, members = split_manifest(logical)
                for ordinal, body in enumerate(members):
                    key = member_key(kind, values[field], ordinal)
                    previous = super().stage('members_get', uow, {'member_key': key})
                    if previous and previous[0]['body'] == body: continue
                    super().stage('members_update' if previous else 'members_insert', uow,
                        {'member_key': key, 'owner_kind': kind, 'owner_id': values[field], 'ordinal': ordinal, 'body': body})
                rows = super().stage(name, uow, {**values, 'manifest': header})
                return tuple(MappingProxyType({**row, 'manifest': logical}) for row in rows)
            rows = super().stage(name, uow, parameters)
            if not name.endswith('_get') or not rows: return rows
            row = rows[0]; header = cast(str, row['manifest']); members = []
            count = super().stage('members_count', uow, {'owner_kind': kind, 'owner_id': row[field]})[0]['count']
            if count != len(member_digests(header)): raise InvalidValue()
            for ordinal in range(len(member_digests(header))):
                leaf = super().stage('members_get', uow, {'member_key': member_key(kind, cast(str, row[field]), ordinal)})
                if not leaf or (leaf[0]['owner_kind'], leaf[0]['owner_id'], leaf[0]['ordinal']) != (kind, row[field], ordinal): raise InvalidValue()
                members.append(cast(str, leaf[0]['body']))
            return (MappingProxyType({**row, 'manifest': join_manifest(header, tuple(members))}),)
        except InvalidValue as failure:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE') from failure

    async def read(self, name: str, parameters: object) -> tuple[MappingProxyType[str, Value], ...]:
        try:
            rows = await super().read(name, parameters)
            if name not in ('preparations_get', 'batches_get') or not rows: return rows
            kind = name.split('_', 1)[0]; field = 'preparation_id' if kind == 'preparations' else 'batch_id'
            row = rows[0]; header = cast(str, row['manifest']); members = []
            count = (await super().read('members_count', {'owner_kind': kind, 'owner_id': row[field]}))[0]['count']
            if count != len(member_digests(header)): raise InvalidValue()
            for ordinal in range(len(member_digests(header))):
                leaf = await super().read('members_get', {'member_key': member_key(kind, cast(str, row[field]), ordinal)})
                if not leaf or (leaf[0]['owner_kind'], leaf[0]['owner_id'], leaf[0]['ordinal']) != (kind, row[field], ordinal): raise InvalidValue()
                members.append(cast(str, leaf[0]['body']))
            if await super().read(name, parameters) != rows:
                raise OwnerFailure('PRECONDITION_FAILED', 'source', 'SOURCE_CHANGED')
            return (MappingProxyType({**row, 'manifest': join_manifest(header, tuple(members))}),)
        except InvalidValue as failure:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE') from failure
