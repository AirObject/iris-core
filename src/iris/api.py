"""Local host API and the service-owned learning scheduler."""
from __future__ import annotations

import ipaddress
import asyncio
import base64
import binascii
import inspect
import json
import sqlite3
from contextlib import asynccontextmanager, ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, Query, Request
from fastapi.security import HTTPBearer
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .tokens import Tokens, install_tokens
from .db import Store
from .models import Gateway
from .model_health import ModelHealth
from .queue import add_message, other_entry_pending
from .media import DEFAULT_MAX_BYTES, MediaError, save_host_media, require_host_media
from .retrieval import Retrieval
from .scheduler import Scheduler
from .service_status import service_status, add_health_hints
from .process_lock import StoreLease
from .admin import install_admin, NewGoal, GoalPatch, GoalQuery, GoalId
from .trial import TrialReplies
from .auth import Sessions, install_auth
from .configuration import RuntimeConfig
from .settings_api import install_settings
from .memory_ops import operation
from .goals import Goals, GoalError, GoalConflict, GoalMerged, goal_list, goal_partition
from .state import CurrentState, PutReport, PatchReport, SourceReport, StateError, MAX_REPORT_BYTES


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
    media_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=100, description="上传接口返回的媒体对象 ID，按消息展示顺序排列")

    @field_validator("media_ids")
    @classmethod
    def unique_media(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("媒体标识不能重复")
        return value

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
    entry_id: str | None = Field(default=None, min_length=1, max_length=200, description="查询入口；校验令牌范围，省略时不传递入口上下文")

    @field_validator("entry_id")
    @classmethod
    def valid_entry(cls, value):
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("入口 ID 不能为空白或含 NUL")
        return value

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


class MediaUpload(Input):
    entry_id: str = Field(min_length=1, max_length=200, description="媒体所属入口；引用该对象也须有权访问此入口")
    content_type: str = Field(max_length=100, description="文件 MIME 类型，须与文件头匹配")
    data_base64: str = Field(description="标准 Base64，不含 data: 前缀或换行；解码后最多 10 MiB")
    understanding_text: str | None = Field(default=None, description="宿主已有理解文本，最多 32768 UTF-8 字节；非空时直接复用")

    @field_validator("entry_id")
    @classmethod
    def valid_entry(cls, value):
        return Search.valid_entry(value)


class HostRequestError(Exception):
    def __init__(self, status, code, message, field="body"):
        self.status, self.code, self.message, self.field = status, code, message, field


class Feedback(Input):
    recall_id: str = Field(min_length=1, max_length=100)
    memory_ids: list[Annotated[int, Field(gt=0)]] = Field(max_length=100)


class HostGoal(NewGoal):
    host: str | None = Field(default=None, min_length=1, max_length=100, description="兼容字段；来源以令牌绑定名称为准")
    host_key: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("host_key")
    @classmethod
    def nonblank_key(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("宿主去重键不能为空白")
        return value


class HostGoalQuery(GoalQuery):
    entry_id: str | None = Field(default=None, min_length=1, max_length=200)


class HostRetrieval(Retrieval):
    """Apply entry authorization to the existing goal projection hook.

    Memory retrieval and participant resolution retain their original path and
    snapshot; formed memories remain shared character knowledge (design 11.4).
    """
    def __init__(self, store, gateway, scope):
        super().__init__(store, gateway)
        self.scope = scope

    def _goals(self, conn, entry_id, limit, *, participants=(), current=None):
        return goal_partition(conn, entry_id=entry_id, participants=participants,
                              limit=limit, current=current, scope=self.scope)


class NotificationPull(Input):
    after: int = Field(default=0, ge=0, le=9223372036854775807)
    limit: int = Field(default=30, ge=1, le=100)


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
            app.state.current_state = CurrentState(active_store)
            app.state.goals = Goals(active_store, gateway=active_gateway)
            app.state.runtime_config = runtime
            app.state.sessions = Sessions(active_store)
            app.state.tokens = Tokens(active_store)
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

    app = FastAPI(title="Iris 宿主接口", version="1.0.0", lifespan=lifespan,
                  description="接收消息、回复准备、记忆查询和使用反馈。接收不等于学习，召回不等于使用。v1 宿主契约已冻结；仅供本机 Bearer 令牌接入，管理接口不属于 v1 承诺。")
    app.state.ready = False

    @app.middleware("http")
    async def readiness(request, call_next):
        if not app.state.ready:
            return JSONResponse({"error": {"code": "unavailable", "message": "服务尚未就绪", "retry_after_seconds": 1}},
                                status_code=503, headers={"Retry-After": "1"})
        return await call_next(request)

    @app.middleware("http")
    async def structured_body_limit(request, call_next):
        path = request.url.path
        host = path == "/api/v1" or path.startswith("/api/v1/")
        bounded = path == "/admin/api/settings/goals" or path == "/admin/api/goals" or path.startswith("/admin/api/goals/")
        if not app.state.ready:
            return await call_next(request)
        if (host or bounded) and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            limit = MAX_REPORT_BYTES
            if host and path not in ("/api/v1/state", "/api/v1/goals") and not path.startswith("/api/v1/goals/"):
                limit = (14 * 1024 * 1024 if path == "/api/v1/media" else
                         64 * 1024 * 1024 if path.endswith("/messages") else 256 * 1024)
            def too_large():
                return JSONResponse({"error": {"code": "payload_too_large" if host else "invalid_request", "fields": [
                    {"field": "body", "message": f"请求正文超过 {limit} 字节 UTF-8 上限"}]}}, status_code=413 if host else 400)
            length = request.headers.get("content-length", "")
            if length.isdecimal() and (len(length) > 20 or int(length) > limit):
                return too_large()
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    return too_large()
                body.extend(chunk)
            if host and body and request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return JSONResponse({"error": {"code": "invalid_request", "message": "请求须使用 application/json", "fields": [
                    {"field": "header.content-type", "message": "请求须使用 application/json"}]}}, status_code=400)
            # BaseHTTPMiddleware replays this cached body to FastAPI validation.
            request._body = bytes(body)
        return await call_next(request)

    install_auth(app)
    install_settings(app)
    install_tokens(app)

    # Registered last so Host is checked before readiness and every API/static route.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"],
                       www_redirect=False)

    @app.middleware("http")
    async def host_error_envelope(request, call_next):
        # Outer formatting only: Host still gates all readiness/auth/body work.
        response = await call_next(request)
        if not (request.url.path == "/api/v1" or request.url.path.startswith("/api/v1/")) or response.status_code < 400:
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        try:
            parsed = json.loads(body)
            error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
        except (ValueError, UnicodeError):
            error = {}
        status = 400 if response.status_code in (405, 422) else response.status_code
        defaults = {400: ("invalid_request", "请求格式或内容不合法"), 401: ("invalid_token", "需要有效的宿主 Bearer 令牌"),
                    403: ("entry_forbidden", "令牌无权执行此请求"), 404: ("not_found", "对象不存在或已删除"),
                    409: ("revision_conflict", "修订号冲突"), 413: ("payload_too_large", "请求或文件过大"),
                    429: ("rate_limited", "宿主请求过于频繁"), 503: ("unavailable", "服务暂时不可用")}
        code, message = defaults.get(status, ("internal_error", "服务无法完成请求"))
        error.setdefault("code", code)
        error.setdefault("message", message)
        error.setdefault("fields", [])
        if not error["fields"] and (status == 400 or "field" in error):
            field = error.get("field") or ("header.host" if body == b"Invalid host header" else
                    "method" if response.status_code == 405 else "query" if request.method == "GET" else "body")
            error["fields"] = [{"field": field, "message": error["message"]}]
        headers = {k: v for k, v in response.headers.items() if k not in ("content-length", "content-type")}
        error["retry_after_seconds"] = error["retry_at"] = None
        if status in (429, 503):
            seconds = max(1, int(headers.get("retry-after", "1")))
            error.update(retry_after_seconds=seconds, retry_at=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat())
            headers["retry-after"] = str(seconds)
        headers["cache-control"] = "no-store"
        return JSONResponse({"error": error}, status_code=status, headers=headers, background=response.background)

    @app.exception_handler(HostRequestError)
    async def host_request_error(request, error):
        return JSONResponse({"error": {"code": error.code, "message": error.message,
            "fields": [{"field": error.field, "message": error.message}]}}, status_code=error.status)

    @app.exception_handler(MediaError)
    async def media_error(request, error):
        status = {"media_too_large": 413, "invalid_media": 404, "unsafe_media_path": 503}.get(error.code, 400)
        field = {"unsupported_media_type": "body.content_type", "invalid_understanding": "body.understanding_text",
                 "media_too_large": "body.data_base64"}.get(error.code, "body.media_ids")
        return JSONResponse({"error": {"code": error.code, "message": str(error),
            "fields": [{"field": field, "message": str(error)}]}}, status_code=status)

    def rejected_host_write(request, status):
        path = request.url.path
        if request.method != "POST" or not (path == "/api/v1/feedback" or (
                path.startswith("/api/v1/entries/") and path.endswith("/learn"))):
            return
        action = "feedback_rejected" if path == "/api/v1/feedback" else "learn_rejected"
        # Do not read or log the unvalidated body, URL identifiers, or exception text.
        with app.state.store.write() as conn:
            operation(conn, action, "request", "host", {"status": status}, actor=request.state.principal.host)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        rejected_host_write(request, 400)
        return JSONResponse({"error": {"code": "invalid_request", "fields": [
            {"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in error.errors()]}}, status_code=400)

    @app.exception_handler(StateError)
    async def invalid_state(request, error):
        field = error.field if error.field == "body" else "body." + error.field
        return JSONResponse({"error": {"code": "invalid_request", "fields": [
            {"field": field, "message": str(error)}]}}, status_code=400)

    @app.exception_handler(GoalError)
    async def invalid_goal(request, error):
        prefix = "query." if request.method == "GET" else "body."
        field = error.field if error.field == "body" else prefix + error.field
        return JSONResponse({"error": {"code": "invalid_request", "fields": [
            {"field": field, "message": str(error)}]}}, status_code=400)

    @app.exception_handler(GoalConflict)
    async def goal_conflict(request, error):
        return JSONResponse({"error": {"code": "revision_conflict", "message": "目标修订号冲突，请刷新后重试"}}, status_code=409)

    @app.exception_handler(GoalMerged)
    async def goal_merged(request, error):
        return JSONResponse({"error": {"code": "goal_merged", "message": "目标已合并，请读取并明确修改保留的目标",
                                        "canonical_id": error.canonical_id}}, status_code=409)

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

    host_router = APIRouter(dependencies=[Depends(HTTPBearer(auto_error=False))])

    @host_router.post("/api/v1/entries/{entry_id}/messages", summary="接收单条或批量消息")
    def messages(entry_id: str, request: Request, payload: Annotated[Message | list[Message], Body(openapi_examples={
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
                existing = conn.execute("SELECT id FROM messages WHERE entry_id=? AND dedupe_key=?", (entry_id, item.dedupe_key)).fetchone()
                if existing is None:
                    require_host_media(conn, item.media_ids, scope=request.state.principal.scope)
                ids.append(add_message(app.state.store, entry_id=entry_id, _conn=conn, **fields))
            pending = conn.execute("SELECT COUNT(*) FROM messages WHERE entry_id=? AND learning_state IN ('pending','batched')", (entry_id,)).fetchone()[0]
        return {"message_ids": ids, "pending_count": pending}

    @host_router.post("/api/v1/media", status_code=201, summary="上传媒体并绑定所属入口")
    def upload_media(payload: MediaUpload, request: Request):
        principal = request.state.principal
        principal.scope.require(payload.entry_id)
        if len(payload.data_base64) > 4 * ((DEFAULT_MAX_BYTES + 2) // 3):
            raise HostRequestError(413, "media_too_large", "媒体超过单文件 10 MiB 上限", "body.data_base64")
        try:
            data = base64.b64decode(payload.data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HostRequestError(400, "invalid_request", "须为标准 Base64 文件内容", "body.data_base64") from None
        try:
            return save_host_media(app.state.store, data, entry_id=payload.entry_id, scope=principal.scope,
                                   actor=principal.host, content_type=payload.content_type,
                                   understanding_text=payload.understanding_text)
        except OSError:
            raise HostRequestError(503, "unavailable", "媒体存储暂时不可用") from None

    @host_router.post("/api/v1/entries/{entry_id}/learn", summary="请求尽快学习；模型暂停时接受并说明原因")
    def learn(entry_id: str, request: Request):
        return app.state.scheduler.request_learning(entry_id, actor=request.state.principal.host)

    @host_router.post("/api/v1/entries/{entry_id}/prepare", summary="准备回复材料，不生成回复正文",
              description="memories[].subject_annotations 附带 possible_same_as（联系双方、belief）与 roleplay（actor、character、worlds、fictional）。标注在选取和判断之后追加，不包含联系证据原文。")
    def prepare(entry_id: str, payload: Prepare, request: Request):
        result = add_health_hints(HostRetrieval(app.state.store, app.state.gateway, request.state.principal.scope).prepare(entry_id, **payload.model_dump()), app.state.health)
        pending = other_entry_pending(app.state.store, entry_id, scope=request.state.principal.scope)
        if pending:
            result["hints"].append({"code": "other_entries_pending", **pending, "time_basis": "received_at",
                "message": f"另有 {pending['entry_count']} 个入口有尚未学习的新消息（{pending['time_from']}—{pending['time_to']}）"})
        app.state.tokens.bind_recall(result["recall_id"], request.state.principal)
        return result

    @host_router.post("/api/v1/memories/search", summary="筛选并查询记忆；深度读取不恢复遗忘记忆",
              description="memories[].subject_annotations 同 prepare：标明可能是同一人和虚构扮演关系；不改变选取顺序，不返回否认的联系。")
    def search(payload: Search, request: Request):
        principal = request.state.principal
        principal.scope.require(payload.entry_id)
        retrieval = HostRetrieval(app.state.store, app.state.gateway, principal.scope)
        fields = payload.model_dump(exclude={"entry_id"})
        # VS owns Retrieval.search; support either merge order without changing it.
        if payload.entry_id is not None and "entry_id" in inspect.signature(retrieval.search).parameters:
            fields["entry_id"] = payload.entry_id
        result = add_health_hints(retrieval.search(**fields), app.state.health)
        app.state.tokens.bind_recall(result["recall_id"], request.state.principal)
        return result

    @host_router.post("/api/v1/feedback", summary="反馈实际使用的记忆，24 小时内同条只强化一次")
    def feedback(payload: Feedback, request: Request):
        app.state.tokens.require_recall(payload.recall_id, request.state.principal)
        return app.state.retrieval.feedback(payload.recall_id, payload.memory_ids)

    @host_router.get("/api/v1/goals", summary="读取角色共享的目标与询问")
    def goals(query: Annotated[HostGoalQuery, Query()], request: Request):
        with app.state.store.read() as conn:
            return goal_list(conn, current=app.state.goals.clock(), scope=request.state.principal.scope, **query.model_dump())

    @host_router.post("/api/v1/goals", status_code=201, summary="注入目标并返回去重结果")
    def create_goal(payload: HostGoal, request: Request):
        principal = request.state.principal
        principal.scope.require(payload.entry_id)
        result = app.state.goals.create(**{**payload.model_dump(), "host": principal.host}, origin="host", actor=principal.host, scope=principal.scope)
        app.state.scheduler.wake()
        return result

    @host_router.patch("/api/v1/goals/{goal_id}", summary="完成、放弃目标，或修改截止时间与提醒提前量")
    def update_goal(goal_id: GoalId, payload: GoalPatch, request: Request):
        result = app.state.goals.update(goal_id, **payload.model_dump(exclude_unset=True), actor=request.state.principal.host, scope=request.state.principal.scope)
        app.state.scheduler.wake()
        return result

    @host_router.get("/api/v1/notifications", summary="取走已发布提醒；取走不代表送达或目标完成")
    def notifications(query: Annotated[NotificationPull, Query()], request: Request):
        return app.state.goals.pull(**query.model_dump(), scope=request.state.principal.scope)

    @host_router.get("/api/v1/state", summary="读取宿主报告的当前状态")
    def get_state():
        return app.state.current_state.get()

    @host_router.put("/api/v1/state", summary="开始或替换活动；相同活动保留开始时间")
    def put_state(payload: PutReport, request: Request):
        request.state.principal.scope.require(payload.entry_id)
        return app.state.current_state.put(**{**payload.model_dump(exclude_unset=True), "host": request.state.principal.host})

    @host_router.patch("/api/v1/state", summary="更新细节、情绪或心跳")
    def patch_state(payload: PatchReport, request: Request):
        request.state.principal.scope.require(payload.entry_id)
        return app.state.current_state.patch(**{**payload.model_dump(exclude_unset=True), "host": request.state.principal.host})

    @host_router.delete("/api/v1/state", summary="结束活动，保留宿主报告历史")
    def delete_state(request: Request, payload: SourceReport = Body(default=SourceReport())):
        request.state.principal.scope.require(payload.entry_id)
        return app.state.current_state.delete(**{**payload.model_dump(exclude_unset=True), "host": request.state.principal.host})

    @host_router.get("/api/v1/status", summary="服务状态、最近模型调用和入口积压")
    def status(request: Request):
        result = service_status(app.state.store, app.state.scheduler, app.state.health)
        scope = request.state.principal.scope
        if scope.kind != "all":
            result["entries"] = [r for r in result["entries"] if scope.allows(r["entry_id"])]
            result["backlog"] = result["entries"]
            result["batches"] = [r for r in result["batches"] if scope.allows(r["entry_id"])]
            allowed = {r["id"] for r in result["batches"]}
            result["learning_calls_24h"] = [r for r in result["learning_calls_24h"] if r["batch_id"] in allowed]
        return result

    app.include_router(host_router)
    install_admin(app)
    _install_host_openapi(app)
    return app


def _install_host_openapi(app):
    """Document the host wire contract without filtering existing projections.

    Runtime response models would silently strip fields owned by the state,
    retrieval and goal modules. Explicit OpenAPI schemas plus HTTP contract tests
    preserve those responses and permit additive v1 evolution.
    """
    from fastapi.openapi.utils import get_openapi

    def obj(properties, required=None):
        return {"type": "object", "properties": properties,
                "required": list(properties) if required is None else required}

    def array(items):
        return {"type": "array", "items": items}

    def nullable(schema):
        return {"anyOf": [schema, {"type": "null"}]}

    string, integer, boolean, number = ({"type": t} for t in ("string", "integer", "boolean", "number"))
    mapping = {"type": "object"}
    stamp = "2026-10-10T08:00:00+08:00"
    field_error = obj({"field": string, "message": string})
    error_schema = obj({"error": obj({"code": string, "message": string, "fields": array(field_error),
        "retry_after_seconds": nullable(integer), "retry_at": nullable({"type": "string", "format": "date-time"}),
        "canonical_id": integer}, ["code", "message", "fields", "retry_after_seconds", "retry_at"])})
    hint = obj({"code": string, "message": string})
    memory = obj({"id": integer, "content": string, "kind": string, "stance": string,
        "revision": integer, "lifecycle": string, "reason": string, "belief": number, "importance": number,
        "retention": number, "speaker_subject_id": string, "speaker_name": string,
        "about": array(obj({"id": string, "name": string})), "tags": array(string),
        "sources": array(obj({"message_id": integer, "entry_id": nullable(string),
                              "entry_name": nullable(string), "occurred_at": nullable(string)}))})
    goal = obj({"id": integer, "content": string, "kind": string, "origin": string, "state": string,
        "entry_id": nullable(string), "host": nullable(string), "host_key": nullable(string),
        "revision": integer, "people": array(string), "deadline": nullable(string),
        "reminder_minutes": nullable(integer), "effective_reminder_minutes": nullable(integer),
        "created_at": string, "updated_at": string, "closed_at": nullable(string), "closed_by": nullable(string),
        "merged_into": nullable(integer), "deadline_unresolved": boolean, "overdue": boolean, "due_soon": boolean,
        "possible_duplicate": boolean, "possible_duplicate_ids": array(integer),
        "basis_needs_review": boolean, "basis_annotations": array(mapping)})
    state = obj({"activity": string, "activity_updated_at": string, "mood": nullable(string),
        "mood_updated_at": nullable(string), "started_at": string, "start_time_basis": string,
        "updated_at": string, "host": nullable(string), "entry_id": nullable(string),
        "details": {"type": "object", "additionalProperties": obj({"value": {}, "updated_at": string})},
        "duration_seconds": number, "possibly_stale": boolean, "stale_after_minutes": integer})
    state_or_empty = {"anyOf": [{"type": "object", "maxProperties": 0}, state]}
    recent = obj({"id": integer, "entry_id": string, "content": string, "kind": string,
                  "occurred_at": string, "received_at": string, "sender_name": string,
                  "sender_subject_id": string, "learning_state": string, "unlearned": boolean})
    prepare = obj({"persona": obj({"version": nullable(integer), "content": string, "generated_at": nullable(string),
                         "needs_update": boolean, "stale_basis_count": integer}), "memories": array(memory), "recent_messages": array(recent),
        "state": state_or_empty, "goals": array(goal), "hints": array(hint), "judgment": mapping, "recall_id": string})
    search = obj({"memories": array(memory), "hints": array(hint), "recall_id": string,
                  "goals": array(goal), "state": state_or_empty}, ["memories", "hints", "recall_id"])
    media = obj({"id": string, "sha256": string, "content_type": string, "size_bytes": integer,
        "kind": string, "understanding_source": {"enum": ["host", "system", "refused", "unprocessed"], "type": "string"},
        "understanding_text": string, "completed_at": nullable(string), "last_error": nullable(string)})
    goal_example = {"id": 1, "content": "整理观星照片", "kind": "normal", "origin": "host", "state": "open",
        "entry_id": "group-a", "host": "astrbot", "host_key": "goal-1", "revision": 1, "people": [],
        "deadline": None, "reminder_minutes": None, "effective_reminder_minutes": 60,
        "created_at": stamp, "updated_at": stamp, "closed_at": None, "closed_by": None, "merged_into": None,
        "deadline_unresolved": False, "overdue": False, "due_soon": False, "possible_duplicate": False,
        "possible_duplicate_ids": [], "basis_needs_review": False, "basis_annotations": []}
    state_example = {"activity": "观星", "activity_updated_at": stamp, "mood": None,
        "mood_updated_at": None, "started_at": stamp, "start_time_basis": "first_report", "updated_at": stamp,
        "host": "astrbot", "entry_id": "group-a", "details": {}, "duration_seconds": 0.0,
        "possibly_stale": False, "stale_after_minutes": 30}
    base = "/api/v1"
    entry = base + "/entries/{entry_id}"
    # method, path, request example, success schema, success example, description
    contracts = [
        ("post", entry + "/messages", {"sender": "小林", "content": "今晚看星星", "occurred_at": stamp,
            "dedupe_key": "host-message-1", "media_ids": []},
         obj({"message_ids": array(integer), "pending_count": integer}), {"message_ids": [1], "pending_count": 1},
         "接收单条对象或 1—1000 条对象数组；同入口 dedupe_key 重试返回原消息，不覆盖正文或媒体引用。整批原子提交；接收不等于学习。正文最多 32768 UTF-8 字节，JSON 请求体最多 64 MiB。media_ids 引用上传对象，须同时有权访问上传入口和接收入口。"),
        ("post", base + "/media", {"entry_id": "group-a", "content_type": "image/gif",
            "data_base64": "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==", "understanding_text": "星空照片"},
         media, {"id": "media-object-id", "sha256": "0" * 64, "content_type": "image/gif", "size_bytes": 34,
             "kind": "image", "understanding_source": "host", "understanding_text": "星空照片", "completed_at": stamp, "last_error": None},
         "用 application/json 上传标准 Base64 文件，不接收 URL、文件路径或 multipart。解码后单文件最多 10 MiB，整个 JSON 最多 14 MiB。白名单：image/png、image/jpeg、image/gif、image/webp、audio/mpeg、audio/wav、audio/ogg、audio/flac、video/mp4、video/webm；须匹配文件头。可附最多 32768 UTF-8 字节的宿主理解文本。返回对象 ID；上传不触发模型，图片理解在学习前完成，音视频以占位参与。"),
        ("post", entry + "/learn", None, obj({"accepted": boolean, "pending_count": integer, "paused": boolean,
             "reason": nullable(string)}), {"accepted": True, "pending_count": 1, "paused": False, "reason": None},
         "无请求正文；请求尽快学习已收到的消息，模型暂停时仍接受。accepted 表示请求已持久化，不代表批次成功；在 status 观察结果。"),
        ("post", entry + "/prepare", {"text": "今晚聊什么？", "participants": [], "known_memory_ids": [], "judge": False},
         prepare, {"persona": {"version": None, "content": "", "generated_at": None, "needs_update": False, "stale_basis_count": 0},
                   "memories": [], "recent_messages": [], "state": {}, "goals": [],
                   "hints": [], "judgment": {"status": "disabled"}, "recall_id": "recall-id"},
         "返回 persona、memories、recent_messages、state、goals、hints 六个分区，以及 judgment 和 recall_id，不生成回复。仅本入口近期消息；其他授权入口的待学习情况仅以数量和接收时间范围加入 hints。text 省略时自适应近期消息，空字符串表示显式空查询。known_memory_ids 排除已在宿主上下文中的记忆。subject_annotations 标注可能同一人与虚构扮演，不附联系证据原文。模型故障时正常返回并提示降级。"),
        ("post", base + "/memories/search", {"text": "观星", "entry_id": "group-a", "include_forgotten": False,
                                            "include_state": False, "include_goals": False},
         search, {"memories": [], "hints": [], "recall_id": "recall-id"},
         "按文本、人物、类型、立场与时间范围筛选。entry_id 可选，给出时检查令牌权限；不传则省略入口上下文。可见范围由检索层实现；AP 兼容尚无 entry_id 参数的旧检索层，部署入口隐私规则须同时包含 VS 实现。include_forgotten 深度读取不恢复遗忘记忆；不返回已删除记忆。include_goals/include_state 仅在开启时加入对应分区。返回 subject_annotations，不运行召回判断。"),
        ("post", base + "/feedback", {"recall_id": "recall-id", "memory_ids": [1]},
         obj({"recall_id": string, "accepted": array(integer), "strengthened": array(integer)}),
         {"recall_id": "recall-id", "accepted": [1], "strengthened": [1]},
         "只填写实际使用的记忆 ID，允许空数组；须为本令牌召回且在 24 小时内。相同召回和记忆重试不重复强化。accepted 是接受的 ID，strengthened 是本次强化的 ID；已删除记忆返回 404。使用不代表内容可信。"),
        ("get", base + "/state", None, state_or_empty, state_example,
         "读取角色当前活动；无活动返回空对象。details 各字段带更新时间；possibly_stale 提示可能过时，duration_seconds 是持续秒数。这是宿主报告，不是长期记忆或 persona。"),
        ("put", base + "/state", {"activity": "观星", "entry_id": "group-a"}, state_or_empty, state_example,
         "开始或替换当前活动；同名活动保留开始时间。可带 details、mood、带时区的 started_at；省略开始时间以接收时间为准。host 始终以令牌绑定名称为准。JSON 最多 32768 字节。"),
        ("patch", base + "/state", {"details": {"location": "露台"}, "mood": "平静"}, state_or_empty, state_example,
         "局部更新细节、情绪，空对象用作心跳；不能借此替换活动。细节值 null 删除该细节。没有活动时返回空对象。JSON 最多 32768 字节。"),
        ("delete", base + "/state", {"entry_id": "group-a"}, state_or_empty, {},
         "结束当前活动，保留报告历史，返回空对象；允许省略正文或提供来源入口。此操作不删除记忆。"),
        ("get", base + "/goals", {"state": "open", "limit": 30, "offset": 0},
         obj({"items": array(goal), "total": integer, "limit": integer, "offset": integer}),
         {"items": [goal_example], "total": 1, "limit": 30, "offset": 0},
         "按 state、kind、overdue、due_soon、entry_id 筛选目标与询问；先按令牌范围过滤，再计数和分页。全局目标可见。截止时间使用角色时区展示。"),
        ("post", base + "/goals", {"content": "整理观星照片", "entry_id": "group-a", "host_key": "goal-1"},
         obj({"goal": goal, "submitted_id": integer, "dedup": mapping}),
         {"goal": goal_example, "submitted_id": 1, "dedup": {"status": "created", "target_id": None}},
         "注入普通目标或 question；返回去重回执，goal 是保留对象，submitted_id 是本次提交对象。可选 host_key 在绑定宿主名称内去重；重试返回原回执，不更新内容。dedup 可能为 created、merged、possible_duplicate，并可附后台判断状态。JSON 最多 32768 字节。"),
        ("patch", base + "/goals/{goal_id}", {"state": "completed", "expected_revision": 1}, goal,
         {**goal_example, "state": "completed", "revision": 2, "closed_at": stamp, "closed_by": "astrbot"},
         "完成或放弃目标，修改截止时间和提醒提前量。建议携带读取到的 expected_revision，冲突返回 409；已合并目标返回 409 和 canonical_id，重新读取后明确修改保留对象。不能通过此接口编辑目标正文。"),
        ("get", base + "/notifications", {"after": 0, "limit": 30},
         obj({"items": array(mapping), "next_cursor": integer, "has_more": boolean}),
         {"items": [], "next_cursor": 0, "has_more": False},
         "按递增游标读取有权访问的提醒和系统告警；返回即标记已取走，未标记已送达或目标完成。相同 after 重试可重复返回同项，宿主按 ID 去重，处理后保存 next_cursor；has_more 时继续拉取。"),
        ("get", base + "/status", None,
         obj({"service": string, "models": array(mapping), "entries": array(mapping), "backlog": array(mapping),
              "batches": array(mapping), "memory_gap_count": integer, "missing_vectors": integer,
              "usage": mapping, "learning_calls_24h": array(mapping), "learning_latency_24h": mapping,
              "chat_reasoning_effort": nullable(string), "model_health": mapping, "budget": mapping,
              "scheduler": mapping, "timeouts_seconds": mapping}),
         {"service": "ready", "models": [], "entries": [], "backlog": [], "batches": [], "memory_gap_count": 0,
          "missing_vectors": 0, "usage": {"today": {}, "week": {}}, "learning_calls_24h": [],
          "learning_latency_24h": {"count": 0, "p95_ms": None}, "chat_reasoning_effort": None,
          "model_health": {}, "budget": {}, "scheduler": {"running": True, "max_concurrent": 2, "last_error": None},
          "timeouts_seconds": {"learning": 180, "chat": 120, "embedding": 30, "retrieval_query": 2, "recall_judge": 10}},
         "服务状态、用途健康、用量、超时预算与学习积压。entries/backlog、batches 和关联学习调用按令牌范围过滤；模型用量是角色全局统计。批次 succeeded 不保证形成新记忆，应读取 result；abandoned/refused 是记忆缺口。"),
    ]

    def openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        schema["components"]["schemas"]["HostError"] = error_schema
        descriptions = {400: "请求不合法，附字段级说明", 401: "令牌无效或缺失", 403: "无权访问入口或对象",
                        404: "对象不存在或已删除", 409: "修订冲突或目标已合并", 413: "消息、媒体或请求体过大",
                        429: "超过令牌请求速率", 503: "启动、迁移或存储暂不可用；恢复时间为重试建议"}
        for method, path, request_example, response_schema, example, description in contracts:
            operation = schema["paths"][path][method]
            operation["description"] = description
            operation["tags"] = ["宿主 v1"]
            operation["x-request-example"] = {"method": method.upper(), "path": path,
                "query" if method == "get" else "body": request_example}
            if "requestBody" in operation and request_example is not None:
                operation["requestBody"]["content"]["application/json"]["examples"] = {
                    "host": {"summary": "宿主请求示例", "value": request_example}}
            for parameter in operation.get("parameters", []):
                if parameter["in"] == "path":
                    parameter["example"] = "group-a" if parameter["name"] == "entry_id" else 1
                elif request_example and parameter["name"] in request_example:
                    parameter["example"] = request_example[parameter["name"]]
            operation["responses"].pop("422", None)
            status = "201" if method == "post" and path in (base + "/media", base + "/goals") else "200"
            operation["responses"][status] = {"description": "请求成功", "content": {"application/json": {
                "schema": response_schema, "example": example}}}
            for code, explanation in descriptions.items():
                retry = code in (429, 503)
                response = {"description": explanation, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/HostError"}, "example": {"error": {
                        "code": {400: "invalid_request", 401: "invalid_token", 403: "entry_forbidden", 404: "not_found",
                                 409: "revision_conflict", 413: "payload_too_large", 429: "rate_limited", 503: "unavailable"}[code],
                        "message": explanation, "fields": [{"field": "body", "message": explanation}] if code == 400 else [],
                        "retry_after_seconds": 1 if retry else None, "retry_at": stamp if retry else None}}}}}
                if retry:
                    response["headers"] = {"Retry-After": {"description": "建议重试等待秒数", "schema": {"type": "integer", "minimum": 1}}}
                elif code == 401:
                    response["headers"] = {"WWW-Authenticate": {"schema": {"type": "string", "enum": ["Bearer"]}}}
                operation["responses"][str(code)] = response
        app.openapi_schema = schema
        return schema

    app.openapi = openapi
