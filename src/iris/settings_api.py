"""Authenticated setup/settings commands and bounded connection checks."""
from __future__ import annotations

import time
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .auth import audit, error, local
from .db import dumps
from .memory_ops import update_role
from .models import Gateway, ModelConfig, ModelError

CONNECTION_TIMEOUT = 10
PRESETS = [
    {'id': 'ark-glm', 'name': '火山方舟 · GLM（Agent Plan）', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3', 'model': 'glm-5.3-flash', 'reasoning_effort': 'low'},
    {'id': 'deepseek', 'name': 'DeepSeek', 'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-chat', 'reasoning_effort': None},
    {'id': 'local', 'name': 'Ollama（本机）', 'base_url': 'http://127.0.0.1:11434/v1', 'model': '', 'reasoning_effort': None},
]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Role(Input):
    name: str = Field(default='Iris', min_length=1, max_length=100)
    background: str = Field(default='', max_length=16000)
    timezone: str = Field(default='Asia/Shanghai', max_length=100)

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        if not value.strip():
            raise ValueError('名称不能为空')
        return value.strip()

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('时区无效') from None
        return value


class Model(Input):
    enabled: bool = True
    base_url: str = Field(default='', max_length=1000)
    model: str = Field(default='', max_length=200)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    dimensions: int | None = Field(default=None, ge=1, le=65536, strict=True)
    reasoning_effort: str | None = Field(default=None, max_length=50)

    @model_validator(mode='after')
    def valid_endpoint(self):
        if self.enabled:
            try:
                url = urlsplit(self.base_url)
                _ = url.port  # Validate malformed/out-of-range ports.
                valid = url.scheme in ('http', 'https') and url.hostname and not (url.username or url.password or url.query or url.fragment)
            except ValueError:
                valid = False
            if not valid or any(c.isspace() for c in self.base_url) or not self.model.strip():
                raise ValueError('请填写有效 HTTP(S) 接口地址和模型名；地址不能携带凭据或查询参数')
        if self.reasoning_effort is not None and not self.reasoning_effort.strip():
            raise ValueError('推理档位不能为空字符串')
        return self

    def config(self, saved, kind):
        if not self.enabled:
            return None
        key = self.api_key.get_secret_value() if self.api_key is not None else (saved.api_key if saved else '')
        return ModelConfig(self.base_url.rstrip('/'), key, self.model.strip(),
                           self.dimensions if kind == 'embedding' else None,
                           self.reasoning_effort if kind == 'chat' else None)


class Limits(Input):
    daily_token_limit: int | None = Field(default=None, ge=1, strict=True)
    learning_concurrency: int = Field(default=2, ge=1, le=32, strict=True)


def install_settings(app):
    router = APIRouter(prefix='/admin/api')

    def snapshot():
        store = app.state.store
        with store.read() as conn:
            operations = [dict(r) for r in conn.execute('SELECT id,actor,action,created_at FROM admin_operations ORDER BY id DESC LIMIT 30')]
        return {'role': {'name': store.setting('role_name', 'Iris'), 'background': store.setting('background', ''),
                         'timezone': store.setting('timezone', None)},
                'models': app.state.runtime_config.public(),
                'model_source': 'external' if app.state.runtime_config.external_loader else 'local',
                'daily_token_limit': store.setting('daily_token_limit'),
                'learning_concurrency': store.setting('learning_concurrency', 2),
                'health': app.state.health.snapshot(), 'presets': PRESETS, 'operations': operations}

    def sync_models():
        configs = app.state.runtime_config.load()
        for kind in ('chat', 'embedding'):
            app.state.gateway.replace_config(kind, configs.get(kind))

    @router.get('/settings')
    def settings():
        return snapshot()

    @router.post('/setup/complete')
    def complete(payload: Role, request: Request):
        if not local(request):
            return error('local_setup_only', '首次设置只允许从本机完成', 403)
        with app.state.store.write() as conn:
            row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='setup_complete'").fetchone()
            if row and row[0] == 'true':
                return error('already_initialized', '首次设置已完成', 409)
            update_role(app.state.store, payload.name, payload.background, payload.timezone, _conn=conn)
            conn.execute("INSERT OR REPLACE INTO runtime_settings VALUES('setup_complete','true')")
            audit(conn, 'setup_completed')
        return {'ok': True}

    @router.patch('/settings/role')
    def role(payload: Role):
        with app.state.store.write() as conn:
            update_role(app.state.store, payload.name, payload.background, payload.timezone, _conn=conn)
            audit(conn, 'role_saved', {'fields': ['name', 'background', 'timezone']})
        return snapshot()

    @router.put('/settings/models/{kind}')
    def model(kind: Literal['chat', 'embedding'], payload: Model):
        runtime = app.state.runtime_config
        if runtime.external_loader:
            return error('external_config', '模型配置来自外部文件，只读', 409)
        config = payload.config(runtime.load().get(kind), kind)
        runtime.save({kind: config})
        sync_models()
        return snapshot()

    @router.patch('/settings/limits')
    def limits(payload: Limits):
        with app.state.store.write() as conn:
            for key, value in payload.model_dump().items():
                conn.execute('INSERT OR REPLACE INTO runtime_settings VALUES(?,?)', (key, dumps(value)))
            audit(conn, 'limits_saved', payload.model_dump())
        app.state.health.set_daily_token_limit(payload.daily_token_limit)
        app.state.scheduler.set_concurrency(payload.learning_concurrency)
        return snapshot()

    @router.post('/settings/models/{kind}/retry')
    def retry(kind: Literal['chat', 'embedding']):
        app.state.gateway.retry_now(kind)
        with app.state.store.write() as conn:
            audit(conn, 'model_retry', {'purpose': kind})
        return snapshot()

    @router.post('/settings/models/{kind}/test')
    def connection(kind: Literal['chat', 'embedding'], payload: dict):
        runtime = app.state.runtime_config
        current = runtime.load().get(kind)
        if payload and runtime.external_loader:
            return error('external_config', '外部模型配置只能测试已保存的值', 409)
        if payload:
            # Preserve FastAPI's safe validation envelope (never serialize input).
            from pydantic import ValidationError
            try:
                current = Model.model_validate(payload).config(current, kind)
            except ValidationError:
                return error('invalid_request', '模型配置无效，请检查输入')
        if not current:
            return {'ok': False, 'message': '未配置模型', 'category': 'configuration', 'duration_ms': 0}
        active = app.state.gateway.configs.get(kind) == current
        gateway = app.state.gateway if active else Gateway({kind: current}, app.state.store)
        started = time.monotonic()
        try:
            body = ({'messages': [{'role': 'user', 'content': '只输出 JSON：{"ok":true}'}],
                     'max_tokens': 3000, 'response_format': {'type': 'json_object'}} if kind == 'chat'
                    else gateway._embedding_payload('Iris 测试连接'))
            result = gateway._call(kind, 'connection_check', body, probe=True, deadline=gateway.monotonic()+CONNECTION_TIMEOUT)
            valid = (bool((result.get('choices') or [{}])[0].get('message', {}).get('content')) if kind == 'chat'
                     else bool(result.get('data') and result['data'][0].get('embedding')))
            if not valid:
                raise ModelError('configuration', 'invalid model response')
            output = {'ok': True, 'category': 'success', 'message': '连接成功'}
        except ModelError as exc:
            labels = {'authentication': '密钥无效', 'configuration': '配置错误', 'account': '账户问题',
                      'content_rejection': '内容被拒绝', 'retryable': '连接失败', 'paused': '用途已暂停'}
            message = '连接超时' if 'timeout' in exc.summary.lower() else labels.get(exc.category, '连接失败')
            output = {'ok': False, 'category': exc.category, 'message': message}
        except (OSError, ValueError, KeyError, TypeError):
            output = {'ok': False, 'category': 'configuration', 'message': '配置或响应无效'}
        finally:
            if not active:
                gateway.close()
        output['duration_ms'] = round((time.monotonic()-started)*1000)
        return output

    app.include_router(router)
