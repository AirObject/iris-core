"""Typed parameter metadata and immutable records for the definition registry.

Inputs are complete built-in dictionaries. Missing fields stay absent until
registration can report them; NoDefault() explicitly declares no default, while
LiteralDefault(None) declares a null value. Marker records do not validate or
copy their payloads: registration owns validation and nested data isolation.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, TypedDict

type Identifier = str
type Text = str
type DeclaredType = Literal["boolean", "integer", "decimal", "string", "array", "object"]
type MetadataValue = (
    None | bool | int | Decimal | str | list[MetadataValue]
    | tuple[MetadataValue, ...] | dict[str, MetadataValue]
)
type FrozenMetadataValue = (
    None | bool | int | Decimal | str
    | tuple[FrozenMetadataValue, ...] | Mapping[str, FrozenMetadataValue]
)
type IdentifierList = list[Identifier] | tuple[Identifier, ...]


@dataclass(frozen=True, slots=True)
class Declared[T]:
    """Explicitly declare a field payload; registration validates its field-specific shape."""

    value: T


@dataclass(frozen=True, slots=True)
class NotApplicable:
    """Explain why a declaration is inapplicable using nonblank, preserved text."""

    reason: Text


@dataclass(frozen=True, slots=True)
class NoDefault:
    """Explicit absence of a default, distinct from an omitted field or a null literal."""


@dataclass(frozen=True, slots=True)
class LiteralDefault[T]:
    """Declare the sole literal default metadata, with null controlled by nullable."""

    value: T


@dataclass(frozen=True, slots=True)
class Unbounded:
    """Explicit absence of one numeric endpoint; a range must have another bound."""


@dataclass(frozen=True, slots=True)
class Bound:
    """A finite, precisely typed numeric endpoint with an explicit inclusion flag."""

    value: int | Decimal
    inclusive: bool


@dataclass(frozen=True, slots=True)
class RangeDescriptor:
    """Numeric limits whose ordering and nonempty domain are checked at registration."""

    lower: Unbounded | Bound
    upper: Unbounded | Bound


class ParameterDefinitionInput(TypedDict):
    """Complete declaration submitted as an exact dict to RegistryBuilder.register.

    All fields are required. Identifiers are nonempty and contain no whitespace;
    descriptive text must contain a non-whitespace character. Roles, validators,
    and dependencies may be empty; scope and consumers may not. Policy, role,
    replacement, upgrade, and activation metadata are declarations only.
    """

    key: Identifier
    owner_module: Identifier
    schema_revision: Identifier
    type: DeclaredType
    default: NoDefault | LiteralDefault[MetadataValue]
    required: bool
    nullable: bool
    unit: Declared[Identifier] | NotApplicable
    range: Declared[RangeDescriptor] | NotApplicable
    enum: Declared[list[MetadataValue] | tuple[MetadataValue, ...]] | NotApplicable
    validator: IdentifierList
    dependencies: IdentifierList
    scope: IdentifierList
    override_policy: Identifier
    sensitivity: Identifier
    read_roles: IdentifierList
    write_roles: IdentifierList
    apply_mode: Identifier
    activation_group: Declared[Identifier] | NotApplicable
    cost_impact: Text
    migration_impact: Text
    description: Text
    deprecated: bool
    replacement: Declared[Identifier] | NotApplicable
    upgrade_rule: Declared[Text] | NotApplicable
    rationale: Text
    consumers: IdentifierList
    validation_method: Text


@dataclass(frozen=True, slots=True)
class ParameterDefinition:
    """Registry-owned definition with immutable nested metadata and no effective value.

    Successful registration converts arrays and identifier lists to tuples and
    objects to read-only views of private dictionaries. Required and nullable
    describe the parameter's top-level value. Numeric ranges do not constrain
    null, and nested arrays and objects have no element schema. Consumers obtain
    these records through a successfully frozen ReadOnlyRegistry.
    """

    key: Identifier
    owner_module: Identifier
    schema_revision: Identifier
    type: DeclaredType
    default: NoDefault | LiteralDefault[FrozenMetadataValue]
    required: bool
    nullable: bool
    unit: Declared[Identifier] | NotApplicable
    range: Declared[RangeDescriptor] | NotApplicable
    enum: Declared[tuple[FrozenMetadataValue, ...]] | NotApplicable
    validator: tuple[Identifier, ...]
    dependencies: tuple[Identifier, ...]
    scope: tuple[Identifier, ...]
    override_policy: Identifier
    sensitivity: Identifier
    read_roles: tuple[Identifier, ...]
    write_roles: tuple[Identifier, ...]
    apply_mode: Identifier
    activation_group: Declared[Identifier] | NotApplicable
    cost_impact: Text
    migration_impact: Text
    description: Text
    deprecated: bool
    replacement: Declared[Identifier] | NotApplicable
    upgrade_rule: Declared[Text] | NotApplicable
    rationale: Text
    consumers: tuple[Identifier, ...]
    validation_method: Text
