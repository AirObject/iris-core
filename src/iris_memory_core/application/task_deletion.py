"""Bounded internal Task cascade; public admission remains at managed Forget."""

from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.errors import AccessDeniedError, NotReadyError
from iris_memory_core.domain.note import PROMISE_KINDS
from iris_memory_core.domain.task import TaskCurrent

MAX_TASK_DELETION_CHILDREN = 500


def children(tx: Transaction, task: TaskCurrent) -> tuple[tuple[str, str], ...]:
    rows = tx.tasks.deletion_children(task.tenant_id, task.id, limit=MAX_TASK_DELETION_CHILDREN + 1)
    if len(rows) > MAX_TASK_DELETION_CHILDREN:
        raise NotReadyError("Task deletion exceeds its child transaction budget")
    return tuple(sorted(rows))


def protected_reason(tx: Transaction, task: TaskCurrent) -> str | None:
    if task.status not in {"completed", "cancelled", "archived"}:
        return "nonterminal_task"
    if tx.tasks.has_live_child_task(task.tenant_id, task.id):
        return "task_has_live_child_plan"
    if task.completed_us is None:
        revision = tx.tasks.current_task_revision_row(task.id)
        if len(revision.source_refs) > 100:
            raise NotReadyError("Task source verification exceeds its budget")
        for ref in revision.source_refs:
            if ref.get("resource_type") != "note":
                continue
            note = tx.notes.get(str(ref.get("resource_id", "")))
            if note.tenant_id != task.tenant_id:
                raise AccessDeniedError("cross-tenant Task promise source")
            if note.kind in PROMISE_KINDS and note.status != "archived":
                return "unfulfilled_promise"
    return None


def cascade(
    tx: Transaction,
    task: TaskCurrent,
    *,
    erase_content: bool,
    now_us: int,
    actor: str,
    reason_code: str,
) -> tuple[tuple[str, str, int], ...]:
    rows = children(tx, task)
    if erase_content:
        tx.tasks.erase_task_content(task.id, now_us=now_us)
    deleted = []
    for kind, identifier in rows:
        if kind == "cognitive_event":
            event = tx.events.get(identifier)
            if event.tenant_id != task.tenant_id:
                raise AccessDeniedError("cross-tenant Task event")
            CognitiveEventService.cancel_for_forget(
                tx, event, now_us=now_us, actor=actor, reason_code=reason_code
            )
            continue
        if tx.is_tombstoned(task.tenant_id, kind, identifier):
            continue
        if kind == "task_dependency":
            dependency = tx.tasks.get_dependency(identifier)
            if dependency.task_id != task.id:
                raise AccessDeniedError("cross-task deletion dependency")
            revision = dependency.current_revision
        else:
            from iris_memory_core.domain.task import TaskStepCurrent, TaskTriggerCurrent

            child: TaskStepCurrent | TaskTriggerCurrent = (
                tx.tasks.get_step(identifier)
                if kind == "task_step"
                else tx.tasks.get_trigger(identifier)
            )
            if child.tenant_id != task.tenant_id or child.task_id != task.id:
                raise AccessDeniedError("cross-task deletion child")
            revision = child.current_revision
        tx.record_tombstone(
            tenant_id=task.tenant_id,
            resource_type=kind,
            resource_id=identifier,
            reason_code=reason_code,
            deleted_by=actor,
        )
        tx.audit(
            tenant_id=task.tenant_id,
            actor=actor,
            action="memory.erased" if erase_content else "memory.tombstoned",
            resource_type=kind,
            resource_id=identifier,
            reason_code=reason_code,
            details={"parent_type": "task", "parent_id": task.id},
            revision=revision,
        )
        deleted.append((kind, identifier, revision))
    return tuple(deleted)
