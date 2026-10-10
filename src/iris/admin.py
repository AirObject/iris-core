"""Validated local UI API. No host route here generates a reply."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from importlib.resources import files
from typing import Annotated, Literal

from fastapi import APIRouter, Path as PathParameter, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from . import admin_data, trial, people, persona
from .auth import audit, error
from .tokens import NewToken, RateLimits
from .queue import reset_batch, update_entry_settings, pace_parameters, filter_parameters, FILTER_DEFAULTS, PACE
from .memory_ops import edit_memory, delete_memory, manage_memory, purge_memory, recreate_memory, operation, missing_batch_targets
from .retrieval import Retrieval
from .service_status import service_status, add_health_hints


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Name(Input):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("名称不能为空")
        return value.strip()


class NewEntry(Name):
    kind: Literal["group", "private"] = "group"


class EntrySettings(Input):
    pace: str | dict | None = None
    filters: dict | None = None

    @field_validator("pace", mode="before")
    @classmethod
    def valid_pace(cls, value):
        if isinstance(value, str) and value not in PACE:
            raise ValueError("使用预设名称或自定义节奏对象")
        pace_parameters(value)
        return value

    @field_validator("filters", mode="before")
    @classmethod
    def valid_filters(cls, value):
        if not isinstance(value, dict):
            raise ValueError("过滤设置必须为对象")
        filter_parameters({**FILTER_DEFAULTS, **value})
        return value

    @model_validator(mode="after")
    def changes(self):
        if not self.model_fields_set or self.model_fields_set == {"filters"} and not self.filters:
            raise ValueError("至少提供一个修改字段")
        return self


class TrialMessage(Input):
    speaker_id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1)
    dedupe_key: str = Field(min_length=1, max_length=200)

    @field_validator("dedupe_key")
    @classmethod
    def reserved_key(cls, value):
        if value.startswith("trial-reply:"):
            raise ValueError("去重键使用了保留前缀")
        return value

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("消息不能为空")
        return value


class Reply(Input):
    message_id: int = Field(gt=0, strict=True)


class Revision(Input):
    expected_revision: int = Field(gt=0, strict=True)


class Edit(Revision):
    content: str = Field(min_length=1, max_length=1000)


class LifecycleEdit(Revision):
    pinned: bool | None = Field(default=None, strict=True)
    importance: int | None = Field(default=None, ge=0, le=100, strict=True)
    retention: int | None = Field(default=None, ge=0, le=100, strict=True)

    @model_validator(mode="after")
    def changes(self):
        keys = self.model_fields_set - {"expected_revision"}
        if not keys or any(getattr(self, key) is None for key in keys):
            raise ValueError("至少提供一个非空修改字段")
        return self


class Purge(Revision):
    confirm: bool = Field(strict=True)


class Recreate(Revision):
    source_revision: int = Field(gt=0, strict=True)


class Page(Input):
    limit: int = Field(default=30, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1000000)


PersonaId = Annotated[int, PathParameter(gt=0, le=9223372036854775807)]


class PersonaRevision(Input):
    expected_version: int = Field(gt=0, le=9223372036854775807, strict=True)


class PersonaEdit(PersonaRevision):
    content: str = Field(min_length=1, max_length=800)


class PersonaReject(PersonaRevision):
    reason: str = Field(default='administrator rejected', min_length=1, max_length=1000)

    @field_validator('reason')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('拒绝原因不能为空白')
        return value.strip()


class PersonaVersions(Page):
    status: Literal['current', 'pending', 'rejected', 'superseded', 'history'] | None = None
    source: Literal['initial_setting', 'periodic', 'regenerate', 'admin_edit', 'rollback'] | None = None


class PersonaDiff(Input):
    before_version: int = Field(gt=0, le=9223372036854775807)
    after_version: int = Field(gt=0, le=9223372036854775807)


GoalId = Annotated[int, PathParameter(gt=0, le=9223372036854775807)]

def goal_time(value):
    if value is None:
        return value
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        elif datetime.fromisoformat(value).tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError("截止时间须为日期或带时区的 ISO 时间") from None
    return value


class NewGoal(Input):
    content: str = Field(min_length=1, max_length=4000)
    kind: Literal["normal", "question"] = "normal"
    deadline: str | None = Field(default=None, max_length=100)
    reminder_minutes: int | None = Field(default=None, ge=0, le=525600, strict=True)
    people: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=100)
    entry_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("content", "entry_id")
    @classmethod
    def nonblank(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("内容或入口不能为空白")
        return value

    @field_validator("deadline")
    @classmethod
    def valid_deadline(cls, value):
        return goal_time(value)

    @field_validator("people")
    @classmethod
    def unique_people(cls, value):
        values = [item.strip() for item in value]
        if any(not item for item in values) or len(set(values)) != len(values):
            raise ValueError("涉及的人须为不重复的非空主体 ID")
        return values

    @model_validator(mode="after")
    def question_without_schedule(self):
        if self.kind == "question" and (self.deadline is not None or self.reminder_minutes is not None):
            raise ValueError("询问没有截止时间和提醒提前量")
        return self


class GoalPatch(Input):
    expected_revision: int | None = Field(default=None, gt=0, strict=True)
    state: Literal["completed", "abandoned"] | None = None
    deadline: str | None = Field(default=None, max_length=100)
    reminder_minutes: int | None = Field(default=None, ge=0, le=525600, strict=True)

    @field_validator("deadline")
    @classmethod
    def valid_deadline(cls, value):
        return goal_time(value)

    @model_validator(mode="after")
    def changes(self):
        fields = self.model_fields_set - {"expected_revision", "reason"}
        if not fields or "state" in fields and self.state is None:
            raise ValueError("至少提供一个修改字段；状态不能为空")
        return self


class GoalReason(Input):
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator('reason')
    @classmethod
    def nonblank_reason(cls, value):
        if value is not None and not value.strip():
            raise ValueError('原因不能为空白')
        return value.strip() if value is not None else None


class AdminGoalPatch(GoalPatch, GoalReason):
    expected_revision: int = Field(gt=0, strict=True)
    content: str = Field(default=None, min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("目标正文不能为空白")
        return value.strip()


class GoalQuery(Page):
    state: Literal["open", "completed", "abandoned"] | None = None
    kind: Literal["normal", "question"] | None = None
    overdue: bool | None = None
    due_soon: bool | None = None


class AdminGoalQuery(GoalQuery):
    entry_id: str | None = Field(default=None, min_length=1, max_length=200)
    possible_duplicate: bool | None = None
    deadline_from: str | None = Field(default=None, max_length=100)
    deadline_to: str | None = Field(default=None, max_length=100)

    @field_validator("deadline_from", "deadline_to")
    @classmethod
    def valid_deadline(cls, value):
        return goal_time(value)


class GoalDuplicate(GoalReason):
    expected_revision: int = Field(gt=0, strict=True)
    other_revision: int = Field(gt=0, strict=True)


class GoalBasisClear(GoalReason):
    expected_revision: int = Field(gt=0, strict=True)


class NotificationQuery(Page):
    status: Literal["pending", "taken", "cancelled"] | None = None
    goal_id: int | None = Field(default=None, gt=0, le=9223372036854775807)


class PeopleQuery(Page):
    text: str = Field(default="", max_length=100)
    pending_only: bool = False
    include_merged: bool = False


class Alias(Revision):
    alias: str = Field(min_length=1, max_length=100)

    @field_validator("alias")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("别名不能为空")
        return value.strip()


class ConfirmPerson(Revision):
    target_id: str = Field(min_length=1, max_length=100)
    expected_source_revision: int = Field(gt=0, strict=True)
    expected_target_revision: int = Field(gt=0, strict=True)


class MemoryQuery(Input):
    text: str = Field(default="", max_length=1000)
    person_id: str | None = Field(default=None, max_length=100)
    kind: Literal["事件", "事实", "偏好", "关系", "观点", "计划", "自我", "其他"] | None = None
    entry_id: str | None = Field(default=None, max_length=200)
    time_from: str | None = None
    time_to: str | None = None
    lifecycle: Literal["active", "forgotten", "deleted", "all"] = "active"
    pinned: bool | None = None
    sort: Literal["relevance", "time", "retention"] = "time"
    limit: int = Field(default=30, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1000000)

    @field_validator("time_from", "time_to")
    @classmethod
    def valid_time(cls, value):
        if value is not None:
            datetime.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def time_order(self):
        if self.time_from and self.time_to:
            def utc(value):
                stamp = datetime.fromisoformat(value)
                return stamp.replace(tzinfo=stamp.tzinfo or timezone.utc)
            if utc(self.time_from) > utc(self.time_to):
                raise ValueError("开始时间不能晚于结束时间")
        return self


class LearningQuery(Input):
    entry_id: str | None = Field(default=None, max_length=200)
    time_from: str | None = Field(default=None, max_length=40)
    time_to: str | None = Field(default=None, max_length=40)
    limit: int = Field(default=30, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1000000)

    @field_validator("time_from", "time_to")
    @classmethod
    def valid_time(cls, value):
        return MemoryQuery.valid_time(value)


class OperationQuery(Page):
    action: str | None = Field(default=None, max_length=100)
    actor: str | None = Field(default=None, max_length=100)
    object_type: str | None = Field(default=None, max_length=100)
    object_id: str | None = Field(default=None, max_length=200)
    time_from: str | None = Field(default=None, max_length=40)
    time_to: str | None = Field(default=None, max_length=40)

    @field_validator("time_from", "time_to")
    @classmethod
    def valid_time(cls, value):
        return MemoryQuery.valid_time(value)


def install_admin(app):
    @asynccontextmanager
    async def lifespan(app):
        jobs = persona.PersonaJobs(app.state.store, app.state.gateway)
        app.state.persona_jobs = jobs
        try:
            yield
        finally:
            # Included router lifespans close before create_app's gateway/store.
            await asyncio.to_thread(jobs.close)

    router = APIRouter(prefix="/admin/api", tags=["本机管理界面"], lifespan=lifespan)

    def record(action, object_type, object_id, details=None):
        with app.state.store.write() as conn:
            operation(conn, action, object_type, object_id, details)

    @router.get("/trial")
    def trial_catalog():
        return trial.trial_catalog(app.state.store)

    @router.post("/trial/entries", status_code=201)
    def create_entry(payload: NewEntry):
        result = trial.create_entry(app.state.store, payload.name, payload.kind)
        record("trial_entry_created", "entry", result["id"])
        return result

    @router.post("/trial/speakers", status_code=201)
    def create_speaker(payload: Name):
        result = trial.create_speaker(app.state.store, payload.name)
        record("trial_speaker_created", "subject", result["id"])
        return result

    @router.get("/trial/entries/{entry_id}")
    def entry(entry_id: str, before: Annotated[int | None, Query(gt=0)] = None):
        return admin_data.trial_snapshot(app.state.store, entry_id, before=before)

    @router.post("/trial/entries/{entry_id}/messages", status_code=201)
    def receive(entry_id: str, payload: TrialMessage):
        if len(payload.content.encode("utf-8")) > 32768:
            return JSONResponse({"error": {"code": "message_too_large", "message": "正文超过 32KB UTF-8 上限"}}, status_code=413)
        result = trial.receive(app.state.store, entry_id, **payload.model_dump())
        record("trial_message_received", "entry", entry_id, {"message_id": result["message_id"]})
        app.state.scheduler.wake()
        return result

    @router.post("/trial/entries/{entry_id}/learn")
    def learn(entry_id: str):
        with app.state.store.read() as conn:
            trial.require_entry(conn, entry_id)
        return app.state.scheduler.request_learning(entry_id, actor="admin")

    @router.post("/trial/entries/{entry_id}/prepare")
    def prepare(entry_id: str):
        with app.state.store.read() as conn:
            trial.require_entry(conn, entry_id)
        result = add_health_hints(Retrieval(app.state.store, app.state.gateway).prepare(entry_id), app.state.health)
        record("trial_prepare", "entry", entry_id, {"recall_id": result["recall_id"]})
        return result

    @router.post("/trial/entries/{entry_id}/reply")
    def reply(entry_id: str, payload: Reply):
        result = app.state.trial_replies.reply(entry_id, payload.message_id)
        record("trial_reply", "entry", entry_id, {"message_id": result["message"]["id"], "reused": result["reused"]})
        app.state.scheduler.wake()
        return result

    @router.get("/catalog")
    def catalog():
        return admin_data.catalog(app.state.store)

    @router.get("/people")
    def people_list(query: Annotated[PeopleQuery, Query()]):
        return admin_data.list_people(app.state.store, **query.model_dump())

    @router.get("/people/{subject_id}")
    def person_detail(subject_id: str):
        return admin_data.person_detail(app.state.store, subject_id)

    @router.post("/people/links/{link_id}/deny")
    def deny_person_link(link_id: int, payload: Revision):
        return people.deny_link(app.state.store, link_id, **payload.model_dump())

    @router.post("/people/links/{link_id}/confirm")
    def confirm_person_link(link_id: int, payload: ConfirmPerson):
        return people.confirm_link(app.state.store, link_id, **payload.model_dump())

    @router.post("/people/{subject_id}/aliases", status_code=201)
    def add_person_alias(subject_id: str, payload: Alias):
        return people.add_alias(app.state.store, subject_id, **payload.model_dump())

    @router.delete("/people/{subject_id}/aliases/{alias_id}")
    def delete_person_alias(subject_id: str, alias_id: int, payload: Revision):
        return people.delete_alias(app.state.store, subject_id, alias_id, **payload.model_dump())

    @router.get("/memories")
    def memories(query: Annotated[MemoryQuery, Query()]):
        return admin_data.list_memories(app.state.store, **query.model_dump())

    @router.get("/memories/upcoming-deletion")
    def upcoming(query: Annotated[Page, Query()]):
        return admin_data.upcoming_deletion(app.state.store, **query.model_dump())

    @router.get("/memories/{memory_id}")
    def memory(memory_id: int):
        return admin_data.memory_detail(app.state.store, memory_id)

    @router.post("/memories/{memory_id}/annotations/{annotation_id}/confirm")
    def confirm_memory_annotation(memory_id: int, annotation_id: int, payload: Revision):
        from .api import RevisionConflict
        from .consolidation import confirm_annotation
        if not confirm_annotation(app.state.store,memory_id,annotation_id,payload.expected_revision):
            raise RevisionConflict()
        return admin_data.memory_detail(app.state.store,memory_id)

    @router.delete("/memories/{memory_id}/annotations/{annotation_id}")
    def clear_memory_annotation(memory_id: int, annotation_id: int, payload: Revision):
        from .api import RevisionConflict
        from .consolidation import clear_annotation
        if not clear_annotation(app.state.store,memory_id,annotation_id,payload.expected_revision):
            raise RevisionConflict()
        return admin_data.memory_detail(app.state.store,memory_id)

    def require_editable(memory_id):
        detail = admin_data.memory_detail(app.state.store, memory_id)
        if detail["lifecycle"] == "deleted":
            raise KeyError(memory_id)

    @router.patch("/memories/{memory_id}")
    def edit(memory_id: int, payload: Edit):
        from .api import RevisionConflict
        require_editable(memory_id)
        if not edit_memory(app.state.store, memory_id, **payload.model_dump()):
            raise RevisionConflict()
        app.state.scheduler.wake()
        return admin_data.memory_detail(app.state.store, memory_id)

    @router.delete("/memories/{memory_id}")
    def delete(memory_id: int, payload: Revision):
        from .api import RevisionConflict
        require_editable(memory_id)
        if not delete_memory(app.state.store, memory_id, payload.expected_revision):
            raise RevisionConflict()
        return admin_data.memory_detail(app.state.store, memory_id)

    def changed_memory(memory_id, payload, **changes):
        from .api import RevisionConflict
        require_editable(memory_id)
        if not manage_memory(app.state.store, memory_id, **payload.model_dump(), **changes):
            raise RevisionConflict()
        return admin_data.memory_detail(app.state.store, memory_id)

    @router.patch("/memories/{memory_id}/lifecycle")
    def lifecycle(memory_id: int, payload: LifecycleEdit):
        return changed_memory(memory_id, payload)

    @router.post("/memories/{memory_id}/forget")
    def forget(memory_id: int, payload: Revision):
        return changed_memory(memory_id, payload, action="forget")

    @router.post("/memories/{memory_id}/restore")
    def restore(memory_id: int, payload: Revision):
        return changed_memory(memory_id, payload, action="restore")

    @router.post("/memories/{memory_id}/purge")
    def purge(memory_id: int, payload: Purge):
        from .api import RevisionConflict
        admin_data.memory_detail(app.state.store, memory_id)
        result = purge_memory(app.state.store, memory_id, **payload.model_dump())
        if result is None:
            raise RevisionConflict()
        return result

    @router.post("/memories/{memory_id}/recreate", status_code=201)
    def recreate(memory_id: int, payload: Recreate):
        from .api import RevisionConflict
        admin_data.memory_detail(app.state.store, memory_id)
        result = recreate_memory(app.state.store, memory_id, **payload.model_dump())
        if result is None:
            raise RevisionConflict()
        app.state.scheduler.wake()
        return admin_data.memory_detail(app.state.store, result)

    @router.get("/operations")
    def operations(query: Annotated[OperationQuery, Query()]):
        return admin_data.operations(app.state.store, **query.model_dump())

    @router.get("/maintenance")
    def maintenance(query: Annotated[Page, Query()]):
        return admin_data.maintenance_runs(app.state.store, **query.model_dump())

    @router.get("/maintenance/{run_id}")
    def maintenance_report(run_id: int):
        return app.state.scheduler.lifecycle.report(run_id)

    @router.post("/maintenance", status_code=202)
    def maintain(payload: Input):
        return app.state.scheduler.request_maintenance()

    @router.get("/entries")
    def entries():
        return admin_data.learning_entries(app.state.store)

    @router.get("/entries/{entry_id}/settings")
    def learning_settings(entry_id: str):
        return admin_data.entry_learning_settings(app.state.store, entry_id)

    @router.patch("/entries/{entry_id}/settings")
    def change_learning_settings(entry_id: str, payload: EntrySettings):
        result = update_entry_settings(app.state.store, entry_id, **payload.model_dump(exclude_unset=True))
        app.state.scheduler.wake()
        return result

    @router.get("/batches")
    def batches(query: Annotated[LearningQuery, Query()]):
        return admin_data.learning_batches(app.state.store, **query.model_dump())

    @router.get("/batches/{batch_id}")
    def batch(batch_id: int):
        return admin_data.batch_detail(app.state.store, batch_id)

    @router.post("/batches/{batch_id}/relearn")
    def relearn(batch_id: int, payload: Input):
        with app.state.store.write() as conn:
            row = conn.execute("SELECT entry_id,state FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError(batch_id)
            if missing_batch_targets(conn, batch_id):
                return error("batch_targets_cleared", "目标段消息已清理，无法重新学习；记忆缺口仍保留", 409)
            try:
                reset_batch(app.state.store, batch_id, _conn=conn)
            except ValueError:
                return error("batch_conflict", "仅放弃或内容拒绝的批次可以重新学习；请刷新状态", 409)
            audit(conn, "batch_relearn", {"batch_id": batch_id, "entry_id": row["entry_id"], "previous_state": row["state"]})
        app.state.scheduler.wake()
        return {"accepted": True, "batch_id": batch_id, "state": "waiting"}

    @router.get("/memory-gaps")
    def gaps(query: Annotated[LearningQuery, Query()]):
        return admin_data.memory_gaps(app.state.store, **query.model_dump())

    @router.get('/persona')
    def current_persona():
        return admin_data.persona_snapshot(app.state.store)

    @router.get('/persona/versions')
    def persona_versions(query: Annotated[PersonaVersions, Query()]):
        return admin_data.persona_versions(app.state.store, **query.model_dump())

    @router.get('/persona/versions/{version_id}')
    def persona_version(version_id: PersonaId):
        return admin_data.persona_version(app.state.store, version_id)

    @router.get('/persona/diff')
    def persona_diff(query: Annotated[PersonaDiff, Query()]):
        # Missing IDs have the same 404 contract as version details.
        admin_data.persona_version(app.state.store, query.before_version)
        admin_data.persona_version(app.state.store, query.after_version)
        return persona.version_diff(app.state.store, query.before_version, query.after_version)

    @router.get('/persona/self-memories')
    def persona_self_memories(query: Annotated[Page, Query()]):
        return admin_data.persona_self_memories(app.state.store, **query.model_dump())

    @router.get('/persona/attempts')
    def persona_attempts(query: Annotated[Page, Query()]):
        return admin_data.persona_attempts(app.state.store, **query.model_dump())

    @router.get('/persona/attempts/{attempt_id}')
    def persona_attempt(attempt_id: PersonaId):
        return admin_data.persona_attempt(app.state.store, attempt_id)

    @router.put('/persona')
    def edit_persona(payload: PersonaEdit):
        version = persona.admin_edit(app.state.store, **payload.model_dump())
        return admin_data.persona_version(app.state.store, version['id'])

    @router.post('/persona/versions/{version_id}/confirm')
    def confirm_persona(version_id: PersonaId, payload: PersonaRevision):
        admin_data.persona_version(app.state.store, version_id)
        version = persona.confirm_candidate(app.state.store, version_id, **payload.model_dump())
        return admin_data.persona_version(app.state.store, version['id'])

    @router.post('/persona/versions/{version_id}/reject')
    def reject_persona(version_id: PersonaId, payload: PersonaReject):
        admin_data.persona_version(app.state.store, version_id)
        version = persona.reject_candidate(app.state.store, version_id, **payload.model_dump())
        return admin_data.persona_version(app.state.store, version['id'])

    @router.post('/persona/versions/{version_id}/rollback')
    def rollback_persona(version_id: PersonaId, payload: PersonaRevision):
        admin_data.persona_version(app.state.store, version_id)
        version = persona.rollback(app.state.store, version_id, **payload.model_dump())
        return admin_data.persona_version(app.state.store, version['id'])

    @router.post('/persona/regenerate', status_code=202)
    def regenerate_persona(payload: PersonaRevision):
        attempt_id = app.state.persona_jobs.submit(**payload.model_dump())
        return JSONResponse({'accepted': True, 'attempt': admin_data.persona_attempt(app.state.store, attempt_id)},
                            status_code=202, headers={'Location': f'/admin/api/persona/attempts/{attempt_id}'})

    @router.get("/goals")
    def goals(query: Annotated[AdminGoalQuery, Query()]):
        return admin_data.goals(app.state.store, current=app.state.goals.clock(), **query.model_dump())

    @router.post("/goals", status_code=201)
    def create_goal(payload: NewGoal):
        result = app.state.goals.create(**payload.model_dump(), origin="admin", actor="admin")
        app.state.scheduler.wake()
        return result

    @router.get("/goals/{goal_id}")
    def goal(goal_id: GoalId):
        return admin_data.goal(app.state.store, goal_id, current=app.state.goals.clock())

    @router.get("/goals/{goal_id}/sources")
    def goal_sources(goal_id: GoalId, query: Annotated[Page, Query()]):
        return admin_data.goal_sources(app.state.store,goal_id,**query.model_dump())

    @router.get("/goals/{goal_id}/revisions")
    def goal_revisions(goal_id: GoalId, query: Annotated[Page, Query()]):
        return admin_data.goal_revisions(app.state.store,goal_id,**query.model_dump())

    @router.delete("/goals/{goal_id}/basis-annotations/{annotation_id}")
    def clear_goal_basis(goal_id: GoalId, annotation_id: GoalId, payload: GoalBasisClear):
        return app.state.goals.clear_basis_annotation(goal_id,annotation_id,**payload.model_dump(),actor='admin')

    @router.patch("/goals/{goal_id}")
    def edit_goal(goal_id: GoalId, payload: AdminGoalPatch):
        result = app.state.goals.update(goal_id, **payload.model_dump(exclude_unset=True), actor="admin")
        app.state.scheduler.wake()
        return result

    @router.post("/goals/{goal_id}/duplicates/{other_id}/merge")
    def merge_goal(goal_id: GoalId, other_id: GoalId, payload: GoalDuplicate):
        result = app.state.goals.merge(goal_id, other_id, **payload.model_dump(), actor="admin")
        app.state.scheduler.wake()
        return result

    @router.post("/goals/{goal_id}/duplicates/{other_id}/dismiss")
    def dismiss_goal_duplicate(goal_id: GoalId, other_id: GoalId, payload: GoalDuplicate):
        return app.state.goals.dismiss_duplicate(goal_id, other_id, **payload.model_dump(), actor="admin")

    @router.get("/notifications")
    def notifications(query: Annotated[NotificationQuery, Query()]):
        return admin_data.notifications(app.state.store, **query.model_dump())

    @router.get("/tokens")
    def host_tokens():
        return admin_data.host_tokens(app.state.store)

    @router.post("/tokens", status_code=201)
    def create_host_token(payload: NewToken):
        return app.state.tokens.create(**payload.model_dump())

    @router.post("/tokens/{token_id}/revoke")
    def revoke_host_token(token_id: str, payload: Input):
        return app.state.tokens.revoke(token_id)

    @router.patch("/settings/host-tokens")
    def host_token_limits(payload: RateLimits):
        with app.state.store.write() as conn:
            from .db import dumps
            row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='host_tokens'").fetchone()
            values = {**json.loads(row[0]), **payload.model_dump(exclude_unset=True)}
            conn.execute("UPDATE runtime_settings SET value_json=? WHERE key='host_tokens'", (dumps(values),))
            operation(conn, "token_limits_saved", "settings", "host_tokens", values, actor="admin")
        return values

    @router.get("/state")
    def current_state():
        return app.state.current_state.get()

    @router.get("/state/reports")
    def state_reports(query: Annotated[Page, Query()]):
        return admin_data.state_reports(app.state.store, **query.model_dump())

    @router.get("/status")
    def status():
        result = service_status(app.state.store, app.state.scheduler, app.state.health)
        admin_data.add_entry_waits(app.state.store, result["entries"])
        return result

    @app.exception_handler(persona.PersonaConflict)
    async def persona_conflict(request, exc):
        return error('persona_conflict', '当前 persona、候选或依据已变化，请刷新后重试', 409)

    @app.exception_handler(persona.PersonaBusy)
    async def persona_busy(request, exc):
        return error('persona_generation_in_progress', '已有 persona 生成任务或服务正在停止，请查询任务状态', 409)

    @app.exception_handler(trial.TrialBusy)
    async def busy(request, error):
        return JSONResponse({"error": {"code": "reply_in_progress", "message": "本入口正在生成回复，请稍后刷新对话"}}, status_code=409)

    @app.exception_handler(trial.TrialReplyFailed)
    async def failed(request, error):
        return JSONResponse({"error": {"code": "reply_failed", "message": "角色回复失败，原消息已保存；可查看运行状态后重试"}}, status_code=502)

    @app.exception_handler(people.PeopleConflict)
    async def people_conflict(request, exc):
        return error("subject_conflict", str(exc), 409)

    app.include_router(router)
    web = files("iris").joinpath("web")
    app.mount("/assets", StaticFiles(directory=str(web.joinpath("assets")), check_dir=False), name="ui-assets")

    @app.get("/setup", include_in_schema=False)
    @app.get("/login", include_in_schema=False)
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(web.joinpath("index.html")), headers={"Cache-Control": "no-cache"})
