"""Media work and immutable original request descriptors are independent leaves."""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import UnitOfWork, Value
from companion_memory.persistence.owned_statements import BoundStatements, OwnerFailure
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.content_codec import decode_content, encode_content
from companion_memory.provider.values import freeze


class MediaWorkRows(BoundStatements):
    """Reconstruct only the current work or one original closed admission."""
    @staticmethod
    def _descriptor(value: str) -> str:
        owned = freeze(decode_content(value.encode(), 4096), 4096)
        if encode_content(cast(Value, owned), 4096).decode() != value: raise InvalidValue()
        return value

    def stage(self, name: str, uow: UnitOfWork, parameters: object) -> tuple[MappingProxyType[str, Value], ...]:
        try:
            kind = name.rsplit('_', 1)[0]
            if kind not in ('work', 'admissions'): return super().stage(name, uow, parameters)
            field = 'original_request_descriptor' if kind == 'work' else 'descriptor'
            if name.endswith(('_insert', '_update')):
                values = cast(dict, parameters); descriptor = self._descriptor(values[field])
                key = {k: values[k] for k in ('work_id', 'admission_generation')}
                existing = super().stage('work_descriptors_get', uow, key)
                if existing:
                    if existing[0]['body'] != descriptor: raise InvalidValue()
                else: super().stage('work_descriptors_insert', uow, {**key, 'body': descriptor})
                rows = super().stage(name, uow, {k: v for k, v in values.items() if k != field})
                return tuple(MappingProxyType({**row, field: descriptor}) for row in rows)
            rows = super().stage(name, uow, parameters)
            if not name.endswith('_get') or not rows: return rows
            row = rows[0]
            descriptor = super().stage('work_descriptors_get', uow, {k: row[k] for k in ('work_id', 'admission_generation')})
            if not descriptor: raise InvalidValue()
            return (MappingProxyType({**row, field: self._descriptor(cast(str, descriptor[0]['body']))}),)
        except InvalidValue as failure:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE') from failure

    async def read(self, name: str, parameters: object) -> tuple[MappingProxyType[str, Value], ...]:
        try:
            rows = await super().read(name, parameters)
            if name not in ('work_get', 'admissions_get') or not rows: return rows
            row = rows[0]
            descriptor = await super().read('work_descriptors_get', {k: row[k] for k in ('work_id', 'admission_generation')})
            if not descriptor: raise InvalidValue()
            if await super().read(name, parameters) != rows:
                raise OwnerFailure('PRECONDITION_FAILED', 'media', 'WORK_FENCED')
            field = 'original_request_descriptor' if name == 'work_get' else 'descriptor'
            return (MappingProxyType({**row, field: self._descriptor(cast(str, descriptor[0]['body']))}),)
        except InvalidValue as failure:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE') from failure
