"""Complete metadata and pure value rules for simulated provider configuration.

No defaults, credentials, persistent revisions or production prices are inferred.
The finite nested structures describe local simulation and shared budget limits.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
import re

from .definitions import Bound, Declared, DeclaredType, NoDefault, NotApplicable, ParameterDefinition


@dataclass(frozen=True, slots=True)
class ProviderRequirement:
    """One required parameter's matching specification, without an effective value."""
    key: str
    type: DeclaredType
    unit: str | None = None
    limits: tuple[int, int] | None = None
    validator: str | None = None
    dependencies: tuple[str, ...] = ()


REQUIREMENTS = tuple(sorted((
    ProviderRequirement("provider.max_in_flight", "integer", "requests", (1, 8), "provider_resource_limits",
                        ("provider.result_max_bytes", "storage.command_max_bytes", "storage.receipt_max_bytes")),
    ProviderRequirement("provider.request_timeout_ms", "integer", "milliseconds", (1, 60000)),
    ProviderRequirement("provider.close_timeout_ms", "integer", "milliseconds", (1, 60000)),
    ProviderRequirement("provider.retry_delay_ms", "integer", "milliseconds", (0, 60000)),
    ProviderRequirement("provider.request_max_bytes", "integer", "bytes", (256, 1048576)),
    ProviderRequirement("provider.result_max_bytes", "integer", "bytes", (256, 8192)),
    ProviderRequirement("provider.query_row_limit", "integer", "rows", (1, 1000)),
    ProviderRequirement("provider.accounts", "array", validator="provider_accounts", dependencies=("provider.max_in_flight",)),
    ProviderRequirement("provider.profiles", "array", validator="provider_profiles", dependencies=("provider.accounts", "provider.request_timeout_ms")),
    ProviderRequirement("provider.role_profiles", "object", validator="provider_role_profiles", dependencies=("provider.profiles",)),
), key=lambda item: item.key))
BINDINGS = {item.validator: item for item in REQUIREMENTS if item.validator}
ROLES = ("LEARNING", "DREAM", "PERSONA", "MEDIA", "EMBEDDING", "RERANK", "GOAL", "DIAGNOSTIC")
CAPABILITIES = ("GENERATION", "EMBEDDING", "RERANK", "MEDIA_UNDERSTANDING")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def identifier(value: object) -> bool:
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


def integer(value: object, lower: int, upper: int) -> bool:
    return type(value) is int and lower <= value <= upper


def record(value: object, names: set[str]) -> bool:
    return type(value) is MappingProxyType and set(value) == names


def matches(definition: ParameterDefinition, expected: ProviderRequirement) -> bool:
    """Compare behavioral metadata; explanatory text and schema IDs are opaque."""
    if (definition.key != expected.key or definition.type != expected.type or definition.owner_module != "provider"
            or "provider" not in definition.consumers or not definition.required or definition.nullable
            or type(definition.default) is not NoDefault or set(definition.read_roles) != {"trusted_operator"}
            or set(definition.write_roles) != {"trusted_operator"} or definition.apply_mode != "INITIALIZE_ONLY"
            or type(definition.activation_group) is not NotApplicable or type(definition.enum) is not NotApplicable):
        return False
    if expected.unit is None:
        if type(definition.unit) is not NotApplicable:
            return False
    elif type(definition.unit) is not Declared or definition.unit.value != expected.unit:
        return False
    if expected.limits is None:
        return type(definition.range) is NotApplicable
    if type(definition.range) is not Declared:
        return False
    return all(type(bound) is Bound and type(bound.value) is int and bound.inclusive and bound.value == limit
               for bound, limit in zip((definition.range.value.lower, definition.range.value.upper), expected.limits))


def accounts_valid(value: object, maximum: object) -> bool:
    if type(value) is not tuple or not 1 <= len(value) <= 4 or not integer(maximum, 1, 8):
        return False
    seen: set[str] = set()
    for raw in value:
        if not record(raw, {"account_id", "window_id", "currency", "max_in_flight", "attempt_limit", "cost_limit_atoms"}):
            return False
        item = cast(MappingProxyType[str, object], raw)
        if (not identifier(item["account_id"]) or not identifier(item["window_id"]) or item["currency"] != "TEST"
                or not integer(item["max_in_flight"], 1, cast(int, maximum))
                or not integer(item["attempt_limit"], 1, 1000000) or not integer(item["cost_limit_atoms"], 0, 10**12)):
            return False
        key = cast(str, item["account_id"])
        if key in seen:
            return False
        seen.add(key)
    return True


def profiles_valid(value: object, accounts: object, timeout: object) -> bool:
    if type(value) is not tuple or not 1 <= len(value) <= 16 or type(accounts) is not tuple or not integer(timeout, 1, 60000):
        return False
    account_ids = set()
    for account in accounts:
        if type(account) is not MappingProxyType or not identifier(account.get("account_id")):
            return False
        account_ids.add(account["account_id"])
    names = {"profile_id", "account_id", "model_id", "wire_protocol", "capability", "max_attempts", "attempt_timeout_ms",
             "max_input_units", "max_output_units", "max_items", "input_price_atoms", "output_price_atoms", "dimensions", "space_id", "media_tasks"}
    seen: set[str] = set()
    for raw in value:
        if not record(raw, names):
            return False
        p = cast(MappingProxyType[str, object], raw)
        if any(not identifier(p[key]) for key in ("profile_id", "account_id", "model_id")):
            return False
        key = cast(str, p["profile_id"])
        if key in seen or p["account_id"] not in account_ids or p["wire_protocol"] != "SIMULATED" or p["capability"] not in CAPABILITIES:
            return False
        seen.add(key)
        for name, lower, upper in (("max_attempts", 1, 4), ("attempt_timeout_ms", 1, cast(int, timeout)),
                                   ("max_input_units", 1, 1048576), ("max_output_units", 0, 1048576),
                                   ("max_items", 1, 64), ("input_price_atoms", 0, 10**6), ("output_price_atoms", 0, 10**6)):
            if not integer(p[name], lower, upper):
                return False
        capability = p["capability"]
        if capability == "GENERATION":
            if p["max_output_units"] == 0 or p["dimensions"] is not None or p["space_id"] is not None or p["media_tasks"] != ():
                return False
        else:
            if p["max_output_units"] != 0 or p["output_price_atoms"] != 0:
                return False
            if capability == "EMBEDDING":
                if not integer(p["dimensions"], 1, 1024) or not identifier(p["space_id"]):
                    return False
            elif p["dimensions"] is not None or p["space_id"] is not None:
                return False
            if capability in ("EMBEDDING", "RERANK"):
                if p["media_tasks"] != () or cast(int, p["max_input_units"]) < cast(int, p["max_items"]):
                    return False
            else:
                tasks = p["media_tasks"]
                if p["max_items"] != 1 or type(tasks) is not tuple or not 1 <= len(tasks) <= 3:
                    return False
                pairs = set()
                for task in tasks:
                    if not record(task, {"modality", "task"}):
                        return False
                    pair = (task["modality"], task["task"])
                    if pair not in (("IMAGE", "DESCRIBE"), ("AUDIO", "TRANSCRIBE"), ("VIDEO", "DESCRIBE")) or pair in pairs:
                        return False
                    pairs.add(pair)
        if cast(int, p["max_input_units"]) * cast(int, p["input_price_atoms"]) + cast(int, p["max_output_units"]) * cast(int, p["output_price_atoms"]) > 2**63-1:
            return False
    return True


def roles_valid(value: object, profiles: object) -> bool:
    if type(value) is not MappingProxyType or not 1 <= len(value) <= 8 or type(profiles) is not tuple:
        return False
    ids = set()
    for profile in profiles:
        if type(profile) is not MappingProxyType or not identifier(profile.get("profile_id")):
            return False
        ids.add(profile["profile_id"])
    for role, values in value.items():
        if role not in ROLES or type(values) is not tuple or not 1 <= len(values) <= 16:
            return False
        if any(not identifier(item) for item in values) or len(set(values)) != len(values) or any(item not in ids for item in values):
            return False
    return True


def validate(identifier: str, value: object, dependencies: MappingProxyType[str, object]) -> str | None:
    """Run one statically bound predicate without changing any candidate value."""
    try:
        if identifier == "provider_accounts":
            return None if accounts_valid(value, dependencies["provider.max_in_flight"]) else "ACCOUNTS_INVALID"
        if identifier == "provider_profiles":
            return None if profiles_valid(value, dependencies["provider.accounts"], dependencies["provider.request_timeout_ms"]) else "PROFILES_INVALID"
        if identifier == "provider_role_profiles":
            return None if roles_valid(value, dependencies["provider.profiles"]) else "ROLE_PROFILES_INVALID"
        if identifier == "provider_resource_limits":
            command, receipt, result = (dependencies[key] for key in ("storage.command_max_bytes", "storage.receipt_max_bytes", "provider.result_max_bytes"))
            valid = (type(command) is int and type(receipt) is int and type(result) is int
                     and command >= 57344 and receipt >= 57344 and 6 * result + 8192 <= receipt)
            return None if valid else "STORAGE_CAPACITY_INSUFFICIENT"
        return "VALIDATOR_FAILED"
    except MemoryError:
        raise
    except Exception:
        return "VALIDATOR_FAILED"


def run_validator(name: str, value: object, dependencies: MappingProxyType[str, object]) -> str | None:
    """Contain unexpected validator failures and reject non-protocol return values."""
    allowed = {"provider_accounts": "ACCOUNTS_INVALID", "provider_profiles": "PROFILES_INVALID",
               "provider_role_profiles": "ROLE_PROFILES_INVALID", "provider_resource_limits": "STORAGE_CAPACITY_INSUFFICIENT"}
    try:
        result = validate(name, value, dependencies)
    except MemoryError:
        raise
    except Exception:
        return "VALIDATOR_FAILED"
    if result is None:
        return None
    if type(result) is str and result in (allowed.get(name), "VALIDATOR_FAILED"):
        return result
    return "VALIDATOR_FAILED"
