"""SQLite configuration history, lifecycle CAS, and serving-generation binding."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace

from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.provider_configs import (
    EmbeddingDefinition,
    ProviderConfig,
    ProviderConfigRevision,
    ProviderProbe,
    ProviderSecret,
    ProviderServing,
)


class ProviderConfigRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        self._connection.execute("SAVEPOINT provider_configuration")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK TO provider_configuration")
            self._connection.execute("RELEASE provider_configuration")
            raise
        self._connection.execute("RELEASE provider_configuration")

    def available(self) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='provider_configs' AND type='table'"
            ).fetchone()
            is not None
        )

    @staticmethod
    def _config(row: sqlite3.Row) -> ProviderConfig:
        values = dict(row)
        values.pop("provider_kind")
        return ProviderConfig(**values)

    def get(self, tenant_id: str, identifier: str) -> ProviderConfig | None:
        if not self.available():
            return None
        row = self._connection.execute(
            "SELECT * FROM provider_configs WHERE tenant_id=? AND id=?", (tenant_id, identifier)
        ).fetchone()
        return self._config(row) if row is not None else None

    def rebuild_ids(
        self, tenant_id: str, *, after: tuple[int, str] | None = None, limit: int = 51
    ) -> tuple[str, ...]:
        if not 1 <= limit <= 101:
            raise ValueError("provider rebuild page exceeds bound")
        clause = " AND (o.created_us,o.id)<(?,?)" if after else ""
        rows = self._connection.execute(
            "SELECT o.id FROM console_operations o JOIN console_operation_providers p "
            "ON p.operation_id=o.id WHERE o.tenant_id=? AND p.action IN ('activate','rollback')"
            + clause
            + " ORDER BY o.created_us DESC,o.id DESC LIMIT ?",
            (tenant_id, *(after or ()), limit),
        )
        return tuple(str(row[0]) for row in rows)

    def list_configs(
        self, tenant_id: str, *, limit: int = 51, after: tuple[int, str] | None = None
    ) -> tuple[ProviderConfig, ...]:
        if not 1 <= limit <= 101:
            raise ValueError("provider configuration page exceeds bound")
        if not self.available():
            return ()
        if after is None:
            rows = self._connection.execute(
                "SELECT * FROM provider_configs WHERE tenant_id=? ORDER BY created_us "
                "DESC,id DESC LIMIT ?",
                (tenant_id, limit),
            )
        else:
            rows = self._connection.execute(
                "SELECT * FROM provider_configs WHERE tenant_id=? AND "
                "(created_us,id)<(?,?) ORDER BY created_us DESC,id DESC LIMIT ?",
                (tenant_id, *after, limit),
            )
        return tuple(self._config(row) for row in rows)

    def _insert_revision(self, revision: ProviderConfigRevision) -> None:
        secret = revision.secret
        if (revision.definition.adapter == "openai-compatible") != (secret is not None):
            raise ConflictError("provider adapter and secret mode differ")
        self._connection.execute(
            "INSERT INTO provider_config_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision.tenant_id,
                revision.config_id,
                revision.content_revision,
                revision.definition.encode(),
                revision.content_hash(),
                secret.mode if secret else "none",
                secret.reference if secret else None,
                secret.digest_prefix if secret else "",
                secret.hint if secret else "",
                revision.created_us,
                revision.created_by,
            ),
        )
        if secret is not None and secret.mode == "sealed":
            if secret.ciphertext is None:
                raise ConflictError("provider sealed secret is missing")
            self._connection.execute(
                "INSERT INTO provider_sealed_secrets "
                "(tenant_id,config_id,content_revision,ciphertext) VALUES (?,?,?,?)",
                (
                    revision.tenant_id,
                    revision.config_id,
                    revision.content_revision,
                    secret.ciphertext,
                ),
            )

    def insert(self, config: ProviderConfig, revision: ProviderConfigRevision) -> None:
        if (
            config.status != "draft"
            or config.revision != 1
            or config.content_revision != 1
            or (config.tenant_id, config.id, config.content_revision)
            != (revision.tenant_id, revision.config_id, revision.content_revision)
        ):
            raise ConflictError("new provider configuration must be one draft revision")
        values = asdict(config)
        with self._atomic():
            self._connection.execute(
                f"INSERT INTO provider_configs ({','.join(values)}) "
                f"VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
            self._insert_revision(revision)

    def revision(
        self, tenant_id: str, config_id: str, content_revision: int
    ) -> ProviderConfigRevision | None:
        if not self.available():
            return None
        row = self._connection.execute(
            "SELECT r.*,s.ciphertext FROM provider_config_revisions r LEFT JOIN "
            "provider_sealed_secrets s USING(tenant_id,config_id,content_revision) WHERE "
            "r.tenant_id=? AND r.config_id=? AND r.content_revision=?",
            (tenant_id, config_id, content_revision),
        ).fetchone()
        if row is None:
            return None
        secret = None
        if row["secret_mode"] != "none":
            secret = ProviderSecret(
                mode=row["secret_mode"],
                reference=row["secret_reference"],
                ciphertext=row["ciphertext"],
                digest_prefix=row["secret_digest_prefix"],
                hint=row["secret_hint"],
            )
            if secret.mode == "sealed" and secret.ciphertext is None:
                raise ConflictError(
                    "provider secret is unavailable", details={"kind": "secret_unavailable"}
                )
        result = ProviderConfigRevision(
            tenant_id=tenant_id,
            config_id=config_id,
            content_revision=content_revision,
            definition=EmbeddingDefinition.decode(row["definition_json"]),
            secret=secret,
            created_us=row["created_us"],
            created_by=row["created_by"],
        )
        if result.content_hash() != row["content_hash"]:
            raise ConflictError("provider configuration integrity failed")
        return result

    def history(
        self, tenant_id: str, config_id: str, *, before: int | None = None, limit: int = 51
    ) -> tuple[ProviderConfigRevision, ...]:
        if not 1 <= limit <= 101:
            raise ValueError("provider history page exceeds bound")
        rows = self._connection.execute(
            "SELECT content_revision FROM provider_config_revisions WHERE tenant_id=? AND"
            " config_id=? AND content_revision<? ORDER BY content_revision DESC LIMIT ?",
            (
                tenant_id,
                config_id,
                before if before is not None else 9_223_372_036_854_775_807,
                limit,
            ),
        )
        result = tuple(self.revision(tenant_id, config_id, int(row[0])) for row in rows)
        return tuple(row for row in result if row is not None)

    def advance(self, config: ProviderConfig, *, expected_revision: int) -> None:
        if config.revision != expected_revision + 1:
            raise ConflictError("provider revision must advance exactly once")
        changed = self._connection.execute(
            "UPDATE provider_configs SET "
            "status=?,revision=?,content_revision=?,updated_us=?,current_operation_id=?,latest_probe_id=?,last_generation_id=?"
            " WHERE tenant_id=? AND id=? AND revision=?",
            (
                config.status,
                config.revision,
                config.content_revision,
                config.updated_us,
                config.current_operation_id,
                config.latest_probe_id,
                config.last_generation_id,
                config.tenant_id,
                config.id,
                expected_revision,
            ),
        ).rowcount
        if changed != 1:
            raise ConflictError("provider revision moved", details={"kind": "revision_mismatch"})

    def append_revision(
        self, revision: ProviderConfigRevision, *, expected_revision: int, now_us: int
    ) -> ProviderConfig:
        current = self.get(revision.tenant_id, revision.config_id)
        if (
            current is None
            or current.status not in {"draft", "probed"}
            or current.revision != expected_revision
            or revision.content_revision != current.content_revision + 1
        ):
            raise ConflictError(
                "provider draft revision moved", details={"kind": "revision_mismatch"}
            )
        changed = replace(
            current,
            status="draft",
            revision=current.revision + 1,
            content_revision=revision.content_revision,
            updated_us=now_us,
            current_operation_id=None,
            latest_probe_id=None,
        )
        with self._atomic():
            self._insert_revision(revision)
            self.advance(changed, expected_revision=expected_revision)
        return changed

    def insert_probe(self, probe: ProviderProbe) -> None:
        values = asdict(probe)
        self._connection.execute(
            f"INSERT INTO provider_probes ({','.join(values)}) "
            f"VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )

    def probe(self, tenant_id: str, identifier: str) -> ProviderProbe | None:
        row = self._connection.execute(
            "SELECT * FROM provider_probes WHERE tenant_id=? AND id=?", (tenant_id, identifier)
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["ok"] = bool(values["ok"])
        values["normalized"] = None if values["normalized"] is None else bool(values["normalized"])
        return ProviderProbe(**values)

    def serving(self, tenant_id: str) -> ProviderServing | None:
        if not self.available():
            return None
        row = self._connection.execute(
            "SELECT * FROM provider_serving WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        return ProviderServing(**dict(row)) if row is not None else None

    def switch_serving(self, serving: ProviderServing, *, expected_epoch: int) -> None:
        if serving.epoch != expected_epoch + 1:
            raise ConflictError("provider serving epoch must advance once")
        pointer = self._connection.execute(
            "SELECT generation_id FROM vector_current WHERE tenant_id=?", (serving.tenant_id,)
        ).fetchone()
        active = self.get(serving.tenant_id, serving.config_id)
        if (
            pointer is None
            or pointer[0] != serving.generation_id
            or active is None
            or active.status != "active"
            or active.content_revision != serving.content_revision
        ):
            raise ConflictError("provider serving binding disagrees with active generation")
        if expected_epoch == 0:
            changed = self._connection.execute(
                "INSERT INTO provider_serving VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id)"
                " DO NOTHING",
                tuple(asdict(serving).values()),
            ).rowcount
        else:
            changed = self._connection.execute(
                "UPDATE provider_serving SET "
                "config_id=?,content_revision=?,generation_id=?,epoch=?,updated_us=? "
                "WHERE tenant_id=? AND epoch=?",
                (
                    serving.config_id,
                    serving.content_revision,
                    serving.generation_id,
                    serving.epoch,
                    serving.updated_us,
                    serving.tenant_id,
                    expected_epoch,
                ),
            ).rowcount
        if changed != 1:
            raise ConflictError(
                "provider serving epoch moved", details={"kind": "revision_mismatch"}
            )

    def bind_generation(
        self,
        tenant_id: str,
        generation_id: str,
        config_id: str,
        content_revision: int,
        *,
        now_us: int,
    ) -> None:
        self._connection.execute(
            "INSERT INTO provider_generation_bindings VALUES (?,?,?,?,?)",
            (tenant_id, generation_id, config_id, content_revision, now_us),
        )

    def generation_binding(self, tenant_id: str, generation_id: str) -> tuple[str, int] | None:
        row = self._connection.execute(
            "SELECT config_id,content_revision FROM provider_generation_bindings WHERE "
            "tenant_id=? AND generation_id=?",
            (tenant_id, generation_id),
        ).fetchone()
        return (str(row[0]), int(row[1])) if row is not None else None

    def reserve_probe_budget(
        self,
        tenant_id: str,
        *,
        now_us: int,
        input_chars: int,
        max_attempts: int,
        max_input_chars: int,
        window_us: int,
    ) -> None:
        if min(input_chars, max_attempts, max_input_chars, window_us) <= 0:
            raise ValueError("provider budget values must be positive")
        row = self._connection.execute(
            "SELECT * FROM provider_probe_budgets WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        started, attempts, chars = now_us, 0, 0
        if row is not None and now_us - row["window_start_us"] < window_us:
            started, attempts, chars = row["window_start_us"], row["attempts"], row["input_chars"]
        if attempts + 1 > max_attempts or chars + input_chars > max_input_chars:
            raise ConflictError(
                "provider probe budget exhausted", details={"kind": "provider_budget_exhausted"}
            )
        self._connection.execute(
            "INSERT INTO provider_probe_budgets VALUES (?,?,?,?) ON CONFLICT(tenant_id) "
            "DO UPDATE SET "
            "window_start_us=excluded.window_start_us,attempts=excluded.attempts,input_chars=excluded.input_chars",
            (tenant_id, started, attempts + 1, chars + input_chars),
        )

    def sealed_revisions(self) -> Iterator[ProviderConfigRevision]:
        if not self.available():
            return
        cursor = self._connection.execute(
            "SELECT tenant_id,config_id,content_revision FROM provider_config_revisions "
            "WHERE secret_mode='sealed' ORDER BY tenant_id,config_id,content_revision"
        )
        while rows := cursor.fetchmany(100):
            for row in rows:
                revision = self.revision(row[0], row[1], row[2])
                assert revision is not None
                yield revision

    def replace_ciphertext(self, revision: ProviderConfigRevision, secret: ProviderSecret) -> None:
        old = revision.secret
        if (
            old is None
            or old.mode != "sealed"
            or secret.mode != "sealed"
            or old.digest_prefix != secret.digest_prefix
            or old.hint != secret.hint
            or secret.reference is not None
        ):
            raise ConflictError("provider reseal changed immutable secret identity")
        changed = self._connection.execute(
            "UPDATE provider_sealed_secrets SET ciphertext=? WHERE tenant_id=? AND "
            "config_id=? AND content_revision=? AND ciphertext=?",
            (
                secret.ciphertext,
                revision.tenant_id,
                revision.config_id,
                revision.content_revision,
                old.ciphertext,
            ),
        ).rowcount
        if changed != 1:
            raise ConflictError("provider sealed envelope moved")
