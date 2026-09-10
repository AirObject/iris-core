"""Independent immutable provider configuration errors without submitted content."""
from dataclasses import dataclass
from .resolution_results import ResolutionFieldPath


@dataclass(frozen=True, slots=True)
class ProviderResolutionIssue:
    """One fixed reason and safe structural location."""
    field_path: ResolutionFieldPath
    reason: str


@dataclass(frozen=True, slots=True)
class ProviderResolutionError:
    """A fixed code attributed only to explicit provider configuration checking."""
    code: str
    operation: str
    issues: tuple[ProviderResolutionIssue]


@dataclass(frozen=True, slots=True)
class ProviderResolutionOk[T]:
    """The complete checked value, without activation or persistence claims."""
    value: T


@dataclass(frozen=True, slots=True)
class ProviderResolutionErr:
    """Safe failure; no partially resolved values are published."""
    error: ProviderResolutionError


type ProviderResolutionResult[T] = ProviderResolutionOk[T] | ProviderResolutionErr


def failure(code: str, path: ResolutionFieldPath, reason: str) -> ProviderResolutionErr:
    return ProviderResolutionErr(ProviderResolutionError(code, "resolve_configuration_with_provider_validation",
                                                        (ProviderResolutionIssue(path, reason),)))
