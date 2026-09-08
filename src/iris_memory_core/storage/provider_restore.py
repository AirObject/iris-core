"""Invalidate restored provider authority before removing vector generations."""

import sqlite3


def reset_provider_projection(connection: sqlite3.Connection, *, now_us: int) -> None:
    """Use the caller's restore transaction; preserve content, secrets and history."""
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_configs'"
        ).fetchone()
        is None
    ):
        return
    connection.execute("DELETE FROM provider_serving")
    # Offline restore connections need not enable FK cascades. Delete these
    # derived bindings explicitly before the referenced generation rows.
    connection.execute("DELETE FROM provider_generation_bindings")
    connection.execute(
        "UPDATE provider_configs SET status=CASE "
        "WHEN status IN ('active','retired') OR EXISTS ("
        "SELECT 1 FROM console_operation_providers p WHERE "
        "p.operation_id=provider_configs.current_operation_id AND "
        "(p.action='rollback' OR (p.action='probe' AND "
        "json_extract(p.plan_json,'$.return_status')='retired'))) THEN 'retired' "
        "WHEN status='discarded' THEN 'discarded' ELSE 'draft' END, "
        "revision=revision+1,updated_us=MAX(updated_us,?),"
        "latest_probe_id=NULL,current_operation_id=NULL "
        "WHERE status IN ('active','probed','probing','activating') "
        "OR latest_probe_id IS NOT NULL OR current_operation_id IS NOT NULL",
        (now_us,),
    )


def provider_invariants(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Check logical bindings in a snapshot, without opening secrets or indexes."""
    problems: list[str] = []
    checks = {
        "provider active configuration and serving pointer disagree": (
            "SELECT 1 FROM provider_configs c LEFT JOIN provider_serving s "
            "ON s.tenant_id=c.tenant_id AND s.config_id=c.id "
            "WHERE c.status='active' AND (s.config_id IS NULL OR "
            "s.content_revision!=c.content_revision) LIMIT 1"
        ),
        "provider serving and vector generation disagree": (
            "SELECT 1 FROM provider_serving s LEFT JOIN provider_configs c "
            "ON c.id=s.config_id AND c.tenant_id=s.tenant_id "
            "LEFT JOIN vector_current v ON v.tenant_id=s.tenant_id "
            "LEFT JOIN vector_generations g ON g.id=s.generation_id AND g.tenant_id=s.tenant_id "
            "LEFT JOIN provider_generation_bindings b ON b.tenant_id=s.tenant_id "
            "AND b.generation_id=s.generation_id "
            "WHERE c.id IS NULL OR c.status!='active' OR c.content_revision!=s.content_revision "
            "OR v.generation_id IS NULL OR v.generation_id!=s.generation_id "
            "OR g.id IS NULL OR g.status!='verified' OR b.generation_id IS NULL LIMIT 1"
        ),
        "provider latest probe identity disagrees": (
            "SELECT 1 FROM provider_configs c JOIN provider_probes p ON p.id=c.latest_probe_id "
            "JOIN console_operation_providers o ON o.operation_id=p.operation_id "
            "WHERE c.tenant_id!=p.tenant_id OR c.id!=p.config_id "
            "OR c.content_revision!=p.content_revision OR o.action!='probe' "
            "OR o.config_id!=p.config_id OR o.content_revision!=p.content_revision LIMIT 1"
        ),
        "provider sealed revision has no envelope": (
            "SELECT 1 FROM provider_config_revisions r LEFT JOIN provider_sealed_secrets s "
            "ON s.tenant_id=r.tenant_id AND s.config_id=r.config_id "
            "AND s.content_revision=r.content_revision "
            "WHERE r.secret_mode='sealed' AND s.config_id IS NULL LIMIT 1"
        ),
    }
    for problem, query in checks.items():
        if connection.execute(query).fetchone() is not None:
            problems.append(problem)
    # A limit-only activation may reference a generation's original content
    # revision. Its endpoint/adapter and all six space fields must still match.
    fields = (
        "model",
        "dimension",
        "metric",
        "normalization",
        "template_version",
        "builder_version",
    )
    mismatch = " OR ".join(
        f"json_extract(r.definition_json,'$.space.{field}') IS NOT g.{field} "
        f"OR json_extract(b.definition_json,'$.space.{field}') IS NOT g.{field} "
        f"OR v.{field} IS NOT g.{field}"
        for field in fields
    )
    row = connection.execute(
        "SELECT 1 FROM provider_serving s "
        "JOIN provider_config_revisions r ON r.tenant_id=s.tenant_id AND r.config_id=s.config_id "
        "AND r.content_revision=s.content_revision "
        "JOIN provider_generation_bindings x ON x.tenant_id=s.tenant_id "
        "AND x.generation_id=s.generation_id "
        "JOIN provider_config_revisions b ON b.tenant_id=x.tenant_id AND b.config_id=x.config_id "
        "AND b.content_revision=x.content_revision "
        "JOIN vector_generations g ON g.id=s.generation_id "
        "JOIN vector_current v ON v.tenant_id=s.tenant_id WHERE "
        + mismatch
        + " OR json_extract(r.definition_json,'$.endpoint') IS NOT "
        "json_extract(b.definition_json,'$.endpoint') "
        "OR json_extract(r.definition_json,'$.adapter') IS NOT "
        "json_extract(b.definition_json,'$.adapter') LIMIT 1"
    ).fetchone()
    if row is not None:
        problems.append("provider serving space or origin binding disagrees")
    return tuple(problems)
