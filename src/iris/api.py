"""M1 host API. No scheduling, generation, authentication, or management routes."""
from __future__ import annotations

import ipaddress
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .db import Store
from .models import Gateway
from .queue import add_message
from .retrieval import Retrieval, backlog, latest_models


def loopback_host(host: str) -> str:
    if host == "localhost":
        return "127.0.0.1"
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise ValueError("尚未设置管理员密码，serve 只允许监听本机回环地址。")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Message(Input):
    sender: str = Field(min_length=1, max_length=200)
    content: str
    occurred_at: str
    dedupe_key: str = Field(min_length=1, max_length=300)
    platform: str = Field(default="host", min_length=1, max_length=100)
    entry_name: str | None = None
    entry_kind: str = "group"
    kind: Literal["message", "self_output", "action_result", "event"] = "message"
    account_id: str | None = None
    scene_identity: str | None = None
    quote_author: str | None = None
    quote_author_account_id: str | None = None
    quote_content: str | None = None
    pace: Literal["realtime", "standard", "economy"] = "standard"

    @field_validator("occurred_at")
    @classmethod
    def valid_time(cls, value):
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError("occurred_at needs a timezone offset")
        return value


class Prepare(Input):
    text: str | None = Field(default=None, max_length=8000, description="查询提示；省略时使用本入口最近 5 条消息")
    participants: list[str] | None = Field(default=None, max_length=100, description="主体 ID 或无歧义的名字；空数组不取人物要点")
    known_memory_ids: list[int] = Field(default_factory=list, max_length=1000, description="宿主上下文已经包含的记忆")
    recent_limit: int = Field(default=20, ge=0, le=100)
    memory_limit: int = Field(default=8, ge=0, le=8)
    token_budget: int = Field(default=1500, ge=0, le=1500)
    goal_limit: int = Field(default=10, ge=0, le=10)


class Search(Input):
    text: str = Field(default="", max_length=8000)
    people: list[str] = Field(default_factory=list, max_length=100)
    kinds: list[Literal["事件", "事实", "偏好", "关系", "观点", "计划", "自我", "其他"]] = Field(default_factory=list)
    stances: list[Literal["亲历", "转述", "观点", "推断", "设定"]] = Field(default_factory=list)
    time_from: str | None = None
    time_to: str | None = None
    include_forgotten: bool = False
    include_goals: bool = False
    include_state: bool = False
    limit: int = Field(default=8, ge=0, le=100)

    @field_validator("time_from", "time_to")
    @classmethod
    def valid_time(cls, value):
        if value:
            datetime.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def time_order(self):
        if self.time_from and self.time_to:
            from datetime import timezone
            a, b = datetime.fromisoformat(self.time_from), datetime.fromisoformat(self.time_to)
            if a.replace(tzinfo=a.tzinfo or timezone.utc) > b.replace(tzinfo=b.tzinfo or timezone.utc):
                raise ValueError("time_from must not exceed time_to")
        return self


class Feedback(Input):
    recall_id: str = Field(min_length=1, max_length=100)
    memory_ids: list[Annotated[int, Field(gt=0)]] = Field(max_length=100)


class RevisionConflict(Exception):
    """Reserved for revision-checked writes; M1 host routes do not edit memory prose."""


def create_app(db_path: str | Path = "data/iris.db", *, store: Store | None = None, configs=None,
               gateway: Gateway | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        active_store = store or Store(db_path)
        active_gateway = gateway or (Gateway(configs, active_store) if configs else None)
        app.state.store = active_store
        app.state.retrieval = Retrieval(active_store, active_gateway)
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            if active_gateway and gateway is None:
                active_gateway.close()
            if store is None:
                active_store.close()

    app = FastAPI(title="Iris 宿主接口", version="0.1.0", lifespan=lifespan,
                  description="接收消息、回复准备、记忆查询和使用反馈。接收不等于学习，召回不等于使用。M1 仅供本机接入。")
    app.state.ready = False

    @app.middleware("http")
    async def readiness(request, call_next):
        if not app.state.ready:
            return JSONResponse({"error": {"code": "unavailable", "message": "服务尚未就绪", "retry_after_seconds": 1}},
                                status_code=503, headers={"Retry-After": "1"})
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"error": {"code": "invalid_request", "fields": [
            {"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in error.errors()]}}, status_code=400)

    @app.exception_handler(ValueError)
    async def invalid_content(request, error):
        return JSONResponse({"error": {"code": "invalid_request", "message": str(error)}}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"error": {"code": "not_found", "message": "对象不存在或已删除"}}, status_code=404)

    @app.exception_handler(RevisionConflict)
    async def conflict(request, error):
        return JSONResponse({"error": {"code": "revision_conflict", "message": "修订号冲突"}}, status_code=409)

    @app.exception_handler(sqlite3.OperationalError)
    async def database_unavailable(request, error):
        return JSONResponse({"error": {"code": "unavailable", "message": "数据库暂时不可用", "retry_after_seconds": 1}},
                            status_code=503, headers={"Retry-After": "1"})

    @app.post("/api/v1/entries/{entry_id}/messages", summary="接收单条或批量消息")
    def messages(entry_id: str, payload: Annotated[Message | list[Message], Body(openapi_examples={
        "single": {"summary": "单条消息", "value": {"sender": "小林", "content": "我周末参加观星", "occurred_at": "2026-09-29T10:00:00+08:00", "dedupe_key": "host-1"}}})]):
        items = payload if isinstance(payload, list) else [payload]
        if not items or len(items) > 1000:
            raise ValueError("messages must contain 1..1000 items")
        if any(len(m.content.encode("utf-8")) > 32768 for m in items):
            return JSONResponse({"error": {"code": "message_too_large", "field": "content", "message": "正文超过 32KB UTF-8 上限"}}, status_code=413)
        with app.state.store.write() as conn:
            ids = []
            for item in items:
                fields = item.model_dump()
                fields["entry_name"] = fields["entry_name"] or entry_id
                ids.append(add_message(app.state.store, entry_id=entry_id, _conn=conn, **fields))
            pending = conn.execute("SELECT COUNT(*) FROM messages WHERE entry_id=? AND learning_state IN ('pending','batched')", (entry_id,)).fetchone()[0]
        return {"message_ids": ids, "pending_count": pending}

    @app.post("/api/v1/entries/{entry_id}/prepare", summary="准备回复材料，不调用生成模型")
    def prepare(entry_id: str, payload: Prepare):
        return app.state.retrieval.prepare(entry_id, **payload.model_dump())

    @app.post("/api/v1/memories/search", summary="筛选并查询记忆；深度读取不恢复遗忘记忆")
    def search(payload: Search):
        return app.state.retrieval.search(**payload.model_dump())

    @app.post("/api/v1/feedback", summary="反馈实际使用的记忆，24 小时内同条只强化一次")
    def feedback(payload: Feedback):
        return app.state.retrieval.feedback(payload.recall_id, payload.memory_ids)

    @app.get("/api/v1/status", summary="服务状态、最近模型调用和入口积压")
    def status():
        with app.state.store.read() as conn:
            return {"service": "ready", "models": latest_models(conn), "backlog": backlog(conn)}

    return app
