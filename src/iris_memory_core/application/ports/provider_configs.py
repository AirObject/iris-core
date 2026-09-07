"""Provider lifecycle repositories shared by application services and runtime."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from iris_memory_core.domain.provider_configs import (
    ProviderConfig,
    ProviderConfigRevision,
    ProviderProbe,
    ProviderSecret,
    ProviderServing,
)


class ProviderConfigRepository(Protocol):
    def available(self) -> bool: ...
    def rebuild_ids(
        self, tenant_id: str, *, after: tuple[int, str] | None = None, limit: int = 51
    ) -> tuple[str, ...]: ...
    def get(self, tenant_id: str, identifier: str) -> ProviderConfig | None: ...
    def list_configs(
        self, tenant_id: str, *, limit: int = 51, after: tuple[int, str] | None = None
    ) -> tuple[ProviderConfig, ...]: ...
    def insert(self, config: ProviderConfig, revision: ProviderConfigRevision) -> None: ...
    def revision(
        self, tenant_id: str, config_id: str, content_revision: int
    ) -> ProviderConfigRevision | None: ...
    def history(
        self, tenant_id: str, config_id: str, *, before: int | None = None, limit: int = 51
    ) -> tuple[ProviderConfigRevision, ...]: ...
    def advance(self, config: ProviderConfig, *, expected_revision: int) -> None: ...
    def append_revision(
        self, revision: ProviderConfigRevision, *, expected_revision: int, now_us: int
    ) -> ProviderConfig: ...
    def insert_probe(self, probe: ProviderProbe) -> None: ...
    def probe(self, tenant_id: str, identifier: str) -> ProviderProbe | None: ...
    def serving(self, tenant_id: str) -> ProviderServing | None: ...
    def switch_serving(self, serving: ProviderServing, *, expected_epoch: int) -> None: ...
    def bind_generation(
        self,
        tenant_id: str,
        generation_id: str,
        config_id: str,
        content_revision: int,
        *,
        now_us: int,
    ) -> None: ...
    def generation_binding(self, tenant_id: str, generation_id: str) -> tuple[str, int] | None: ...
    def reserve_probe_budget(
        self,
        tenant_id: str,
        *,
        now_us: int,
        input_chars: int,
        max_attempts: int,
        max_input_chars: int,
        window_us: int,
    ) -> None: ...
    def sealed_revisions(self) -> Iterator[ProviderConfigRevision]: ...
    def replace_ciphertext(
        self, revision: ProviderConfigRevision, secret: ProviderSecret
    ) -> None: ...
