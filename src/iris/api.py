"""Local host API and the service-owned learning scheduler."""
from __future__ import annotations

import ipaddress
import asyncio
import sqlite3
from contextlib import asynccontextmanager, ExitStack
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .db import Store
from .models import Gateway
from .model_health import ModelHealth
from .queue import add_message
from .retrieval import Retrieval
from .scheduler import Scheduler
from .service_status import service_status, add_health_hints
from .process_lock import StoreLease
from .admin import install_admin
from .trial import TrialReplies
from .auth import Sessions, install_auth
from .configuration import RuntimeConfig
from .settings_api import install_settings
from .memory_ops import operation


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


class CustomPace(Input):
    count: int = Field(ge=1, le=1000, strict=True)
    idle_seconds: int = Field(ge=1, le=86400, strict=True)
    max_wait_seconds: int = Field(ge=1, le=604800, strict=True)


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
    pace: Literal["realtime", "standard", "economy"] | CustomPace = "standard"

    @field_validator("occurred_at")
    @classmethod
    def valid_time(cls, value):
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError("occurred_at needs a timezone offset")
        return value


class Prepare(Input):
    judge: bool = Field(default=True, strict=True, description="是否运行召回判断")
    judge_budget_seconds: float = Field(default=10.0, gt=0, le=10, strict=True, allow_inf_nan=False,
                                        description="判断总预算，包含排队；仅可缩短默认 10 秒")
    text: str | None = Field(default=None, max_length=8000, description="查询提示；省略时使用本入口自适应近期消息")
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
               gateway: Gateway | None = None, config_loader=None, learning_concurrency=None, open_browser_url=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        resources = ExitStack()
        try:
            if store is None:
                resources.enter_context(StoreLease(db_path))
            active_store = store or Store(db_path)
            if store is None:
                resources.callback(active_store.close)
            external_loader = config_loader
            if external_loader is None and (configs is not None or gateway is not None):
                external_loader = lambda: configs if configs is not None else gateway.configs
            runtime = RuntimeConfig(active_store, external_loader=external_loader)
            loaded = runtime.load()
            health = getattr(gateway, "health", None) or ModelHealth(active_store, loaded)
            active_gateway = gateway or Gateway(loaded, active_store, health=health)
            if gateway is None:
                resources.callback(active_gateway.close)
            scheduler = Scheduler(active_store, active_gateway, config_loader=runtime.load, max_concurrent=learning_concurrency)
            resources.callback(scheduler.stop)
            app.state.store = active_store
            app.state.runtime_config = runtime
            app.state.sessions = Sessions(active_store)
            app.state.gateway = active_gateway
            app.state.health = health
            app.state.scheduler = scheduler
            app.state.retrieval = Retrieval(active_store, active_gateway)
            app.state.trial_replies = TrialReplies(active_store, active_gateway, health)
            scheduler.start()
            app.state.ready = True
            if open_browser_url and not active_store.setting('setup_complete', False):
                import webbrowser
                # Uvicorn binds after lifespan startup; delay opening without delaying startup.
                from threading import Timer
                opener = Timer(0.7, webbrowser.open, args=(open_browser_url + '/setup',))
                opener.daemon = True
                opener.start()
                resources.callback(opener.cancel)
            yield
        finally:
            app.state.ready = False
            await asyncio.to_thread(resources.close)

    app = FastAPI(title="Iris 宿主接口", version="0.1.0", lifespan=lifespan,
                  description="接收消息、回复准备、记忆查询和使用反馈。接收不等于学习，召回不等于使用。M1 仅供本机接入。")
    app.state.ready = False

    @app.middleware("http")
    async def readiness(request, call_next):
        if not app.state.ready:
            return JSONResponse({"error": {"code": "unavailable", "message": "服务尚未就绪", "retry_after_seconds": 1}},
                                status_code=503, headers={"Retry-After": "1"})
        return await call_next(request)

    install_auth(app)
    install_settings(app)

    # Registered last so Host is checked before readiness and every API/static route.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"],
                       www_redirect=False)

    def rejected_host_write(request, status):
        path = request.url.path
        if request.method != "POST" or not (path == "/api/v1/feedback" or (
                path.startswith("/api/v1/entries/") and path.endswith("/learn"))):
            return
        action = "feedback_rejected" if path == "/api/v1/feedback" else "learn_rejected"
        # Do not read or log the unvalidated body, URL identifiers, or exception text.
        with app.state.store.write() as conn:
            operation(conn, action, "request", "host", {"status": status}, actor="host")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        rejected_host_write(request, 400)
        return JSONResponse({"error": {"code": "invalid_request", "fields": [
            {"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in error.errors()]}}, status_code=400)

    @app.exception_handler(ValueError)
    async def invalid_content(request, error):
        rejected_host_write(request, 400)
        return JSONResponse({"error": {"code": "invalid_request", "message": str(error)}}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        rejected_host_write(request, 404)
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

    @app.post("/api/v1/entries/{entry_id}/learn", summary="请求尽快学习；模型暂停时接受并说明原因")
    def learn(entry_id: str):
        return app.state.scheduler.request_learning(entry_id)

    @app.post("/api/v1/entries/{entry_id}/prepare", summary="准备回复材料，不生成回复正文",
              description="memories[].subject_annotations 附带 possible_same_as（联系双方、belief）与 roleplay（actor、character、worlds、fictional）。标注在选取和判断之后追加，不包含联系证据原文。")
    def prepare(entry_id: str, payload: Prepare):
        return add_health_hints(Retrieval(app.state.store, app.state.gateway).prepare(entry_id, **payload.model_dump()), app.state.health)

    @app.post("/api/v1/memories/search", summary="筛选并查询记忆；深度读取不恢复遗忘记忆",
              description="memories[].subject_annotations 同 prepare：标明可能是同一人和虚构扮演关系；不改变选取顺序，不返回否认的联系。")
    def search(payload: Search):
        return add_health_hints(Retrieval(app.state.store, app.state.gateway).search(**payload.model_dump()), app.state.health)

    @app.post("/api/v1/feedback", summary="反馈实际使用的记忆，24 小时内同条只强化一次")
    def feedback(payload: Feedback):
        return app.state.retrieval.feedback(payload.recall_id, payload.memory_ids)

    @app.get("/api/v1/status", summary="服务状态、最近模型调用和入口积压")
    def status():
        return service_status(app.state.store, app.state.scheduler, app.state.health)

    install_admin(app)
    return app
