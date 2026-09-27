"""Bounded field diagnostics for an administrator's incomplete managed draft.

Only native declarations supply paths; submitted values and unknown keys never
appear in results. This read-only projection cannot issue a candidate or replace
complete resolution, including its cross-field, capacity and resource checks.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TypedDict, cast

from companion_memory.persistence.schema import (
    BoundedTextSchema, RecordSchema, ScalarSchema, SequenceSchema, valid_identifier,
)
from companion_memory.persistence.semantic_records import Schema
from .managed_product_schema import ACCOUNT, EMBEDDING_PROFILE, IMAGE_PROFILE
from .definitions import FrozenMetadataValue, LiteralDefault, ParameterDefinition
from .deployment import DeploymentSettings
from .dream_schema import ROLE_PROFILES, ROLES
from .managed_product_schema import GENERATION_PROFILE
from .managed_registry import registries
from .managed_schema import MATERIAL_VALUES
from .managed_validation import PERSONA_REQUIRED_TEXT, persona_text_missing, TOKEN_PRICE_REQUIRED_FIELDS, token_price_issue
from .resolution import _constraint_failure

ISSUE_LIMIT = 64
NODE_LIMIT = 4096
_MISSING = object()
_NONBLANK_FIELDS = frozenset('text.self_model.initial_persona.' + field for field in PERSONA_REQUIRED_TEXT)


class ConfigurationIssue(TypedDict):
    """A declared field location and a fixed reason, without rejected values."""
    field: str
    reason: str


class ConfigurationDiagnostics(TypedDict):
    """An empty issue list is not proof of valid configuration."""
    issues: list[ConfigurationIssue]
    issues_truncated: bool


class _Collector:
    def __init__(self) -> None:
        self.issues: list[ConfigurationIssue] = []
        self.nodes = 0
        self.truncated = False

    def visit(self) -> bool:
        if self.nodes >= NODE_LIMIT or len(self.issues) >= ISSUE_LIMIT:
            self.truncated = True
            return False
        self.nodes += 1
        return True

    def add(self, path: str, reason: str) -> None:
        if len(self.issues) < ISSUE_LIMIT:
            self.issues.append({'field': path, 'reason': reason})
        else:
            self.truncated = True

    def record(self, value: object, names: tuple[str, ...], path: str) -> dict[str, object] | None:
        if type(value) is not dict:
            self.add(path, 'TYPE_MISMATCH')
            return None
        # Count only known keys. Unknown input keys are neither visited nor echoed.
        if len(value) != sum(name in value for name in names):
            self.add(path, 'UNKNOWN_FIELD')
        return cast(dict[str, object], value)

    def array(self, value: object, minimum: int, maximum: int, path: str) -> list[object] | tuple[object, ...] | None:
        if type(value) is not list and type(value) is not tuple:
            self.add(path, 'TYPE_MISMATCH')
            return None
        if not minimum <= len(value) <= maximum:
            self.add(path, 'ARRAY_LENGTH_INVALID')
            return None
        return value

    def inspect(self, schema: Schema, value: object, path: str, *, nullable: bool = False) -> None:
        if not self.visit():
            return
        if value is _MISSING or value is None and not nullable:
            self.add(path, 'MISSING_REQUIRED')
            return
        if value is None:
            return
        if type(schema) is RecordSchema:
            source = self.record(value, tuple(field.name for field in schema.fields), path)
            if source is None:
                return
            for field in schema.fields:
                if self.truncated:
                    break
                if field.optional and field.name not in source:
                    continue
                self.inspect(field.schema, source.get(field.name, _MISSING),
                    path + '.' + field.name, nullable=field.nullable)
        elif type(schema) is SequenceSchema:
            sequence = self.array(value, schema.minimum, schema.maximum, path)
            if sequence is not None:
                for index, item in enumerate(sequence):
                    if self.truncated:
                        break
                    self.inspect(schema.item, item, f'{path}[{index}]')
        elif type(schema) is BoundedTextSchema:
            if type(value) is not str:
                self.add(path, 'TYPE_MISMATCH')
            elif len(value) > schema.max_utf8_bytes:
                self.add(path, 'TEXT_TOO_LONG')
            else:
                try:
                    size = len(value.encode('utf-8'))
                except UnicodeError:
                    self.add(path, 'INVALID_TEXT')
                    return
                if size > schema.max_utf8_bytes:
                    self.add(path, 'TEXT_TOO_LONG')
                elif path in _NONBLANK_FIELDS and persona_text_missing(value):
                    self.add(path, 'MISSING_REQUIRED')
        elif type(schema) is ScalarSchema:
            if schema.kind == 'identifier':
                if type(value) is str and len(value) <= 128 and not value.strip():
                    self.add(path, 'MISSING_REQUIRED')
                elif not valid_identifier(value):
                    self.add(path, 'INVALID_IDENTIFIER')
            elif schema.kind == 'boolean':
                if type(value) is not bool:
                    self.add(path, 'TYPE_MISMATCH')
            elif schema.kind == 'integer':
                if type(value) is not int:
                    self.add(path, 'TYPE_MISMATCH')
                elif not schema.minimum <= value <= schema.maximum:
                    self.add(path, 'OUT_OF_RANGE')
            elif schema.kind == 'enum':
                if type(value) is not str:
                    self.add(path, 'TYPE_MISMATCH')
                elif not value.strip():
                    self.add(path, 'MISSING_REQUIRED')
                elif value not in schema.choices:
                    self.add(path, 'NOT_IN_ENUM')

    def profiles(self, value: object, path: str) -> None:
        if not self.visit():
            return
        sequence = self.array(value, len(ROLES), len(ROLES), path)
        if sequence is None:
            return
        for index, item in enumerate(sequence):
            if self.truncated:
                break
            location = f'{path}[{index}]'
            if type(item) is not dict:
                self.add(location, 'TYPE_MISMATCH')
                continue
            role = item.get('material_role')
            if type(role) is not str or role not in ROLES:
                self.inspect(ScalarSchema('enum', choices=ROLES), role, location + '.material_role')
                continue
            schema = IMAGE_PROFILE if role == 'MEDIA' else EMBEDDING_PROFILE if role.startswith('EMBEDDING') else GENERATION_PROFILE
            self.inspect(schema, item, location)

    def parameter(self, definition: ParameterDefinition, value: object, path: str) -> None:
        if not self.visit():
            return
        if value is _MISSING:
            if type(definition.default) is LiteralDefault or not definition.required:
                return
            self.add(path, 'MISSING_REQUIRED')
            return
        if value is None:
            if not definition.nullable:
                self.add(path, 'MISSING_REQUIRED')
            return
        key = definition.key
        if key in MATERIAL_VALUES:
            self.inspect(MATERIAL_VALUES[key], value, path)
        elif key == 'provider.accounts':
            self.inspect(SequenceSchema(ACCOUNT, 2, 3), value, path)
            if (type(value) is list or type(value) is tuple) and 2 <= len(value) <= 3:
                for index, account in enumerate(value):
                    if type(account) is not dict or account.get('billing_mode') != 'TOKEN_METERED':
                        continue
                    price = account.get('price')
                    if type(price) is not dict:
                        continue
                    for field in TOKEN_PRICE_REQUIRED_FIELDS:
                        if not self.visit():
                            break
                        location = f'{path}[{index}].price.{field}'
                        reason = token_price_issue(field, price.get(field))
                        if reason is not None and not any(issue['field'] == location for issue in self.issues):
                            self.add(location, reason)
        elif key == 'provider.profiles':
            self.profiles(value, path)
        elif key == 'provider.role_profiles':
            self.inspect(ROLE_PROFILES, value, path)
        elif definition.type in ('object', 'array'):
            expected = (dict,) if definition.type == 'object' else (list, tuple)
            if type(value) not in expected:
                self.add(path, 'TYPE_MISMATCH')
        elif type(value) not in (str, int, bool, Decimal):
            self.add(path, 'TYPE_MISMATCH')
        else:
            error = _constraint_failure(definition, cast(FrozenMetadataValue, value), ())
            if error is not None:
                self.add(path, error.error.issues[0].reason)


def diagnose_managed_configuration(settings: DeploymentSettings, directories: dict[str, tuple[str, ...]],
        platform_id: str, values: object) -> ConfigurationDiagnostics:
    """Project at most 64 issues while visiting at most 4096 declared nodes.

    Call after complete resolution fails, under administrator permission. Only
    trusted declarations determine field names and array bounds. Results contain
    no input values, open no resources, and do not modify or validate a draft.
    Truncation also covers an exhausted traversal budget; no issue is an approval.
    """
    collector = _Collector()
    if not valid_identifier(platform_id):
        collector.add('platform_id', 'INVALID_IDENTIFIER')
    else:
        native = registries(settings, directories, platform_id)
        root = collector.record(values, tuple(native), 'configuration')
        if root is not None:
            for domain, registry in native.items():
                if collector.truncated or not collector.visit():
                    break
                if domain not in root:
                    collector.add(domain, 'MISSING_REQUIRED')
                    continue
                definitions = registry.list_definitions()
                source = collector.record(root[domain], tuple(item.key for item in definitions), domain)
                if source is None:
                    continue
                for definition in definitions:
                    if collector.truncated:
                        break
                    collector.parameter(definition, source.get(definition.key, _MISSING), domain + '.' + definition.key)
    return {'issues': collector.issues, 'issues_truncated': collector.truncated}
