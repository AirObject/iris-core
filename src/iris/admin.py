"""Validated local UI API. No host route here generates a reply."""
from __future__ import annotations

from datetime import datetime, timezone
from importlib.resources import files
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import admin_data, trial, people
from .auth import audit, error
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
    router = APIRouter(prefix="/admin/api", tags=["本机管理界面"])

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

    @router.get("/status")
    def status():
        result = service_status(app.state.store, app.state.scheduler, app.state.health)
        admin_data.add_entry_waits(app.state.store, result["entries"])
        return result

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
