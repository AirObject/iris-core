"""Authenticated setup/settings commands and bounded connection checks."""
from __future__ import annotations

import json
import time
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .auth import audit, error, local
from .db import dumps
from .memory_ops import update_role, lifecycle_settings, operation
from .models import JUDGMENT_KINDS, MODEL_KINDS, Gateway, ModelConfig, ModelError
from .recall_judge import settings as judge_settings
from .persona import DEFAULT_GOAL, DEFAULT_RULES, DEFAULT_PUBLISH_MODE, persona_settings
from .state import state_settings
from .service_status import model_health_snapshot
from .consolidation import consolidation_settings
from .goals import goal_settings
from .goal_dedup_judge import DEFAULTS as GOAL_JUDGE_DEFAULTS, settings as goal_judge_settings

CONNECTION_TIMEOUT = 10
PRESETS = [
    {'id': 'ark-glm', 'name': '火山方舟 · GLM（Agent Plan）', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3', 'model': 'glm-5.3-flash', 'reasoning_effort': 'high'},
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


class ModelKeyRequired(ValueError):
    """A fixed, safe field error; never carries endpoint or credential text."""


def endpoint_origin(url):
    parsed = urlsplit(url)
    return (parsed.scheme.lower(), (parsed.hostname or '').encode('idna').decode('ascii').lower(),
            parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80))


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

    def config(self, saved, kind, *, key_missing=False):
        if not self.enabled:
            return None
        if key_missing and self.api_key is None:
            raise ModelKeyRequired('已保存的模型密钥不可用，请重新输入 API key（无密钥服务须显式传空字符串）')
        if saved and self.api_key is None and endpoint_origin(self.base_url) != endpoint_origin(saved.base_url):
            raise ModelKeyRequired('模型接口 origin 已改变，请重新输入 API key（无密钥服务须显式传空字符串）')
        key = self.api_key.get_secret_value() if self.api_key is not None else (saved.api_key if saved else '')
        effort = self.reasoning_effort
        if kind in JUDGMENT_KINDS and effort is None:
            effort = 'high'
        return ModelConfig(self.base_url.rstrip('/'), key, self.model.strip(),
                           self.dimensions if kind == 'embedding' else None,
                           effort if kind != 'embedding' else None)


class RecallJudge(Input):
    enabled: bool = Field(default=True, strict=True)
    concurrency: int = Field(default=1, ge=1, le=8, strict=True)
    queue_limit: int = Field(default=8, ge=0, le=64, strict=True)


class GoalDedupJudge(RecallJudge):
    enabled: bool = Field(default=GOAL_JUDGE_DEFAULTS['enabled'], strict=True)
    budget_seconds: float = Field(default=GOAL_JUDGE_DEFAULTS['budget_seconds'], gt=0, le=10, strict=True)


class PersonaSettings(Input):
    goal: str = Field(default=DEFAULT_GOAL, min_length=1, max_length=4000, strict=True)
    rules: str = Field(default=DEFAULT_RULES, min_length=1, max_length=16000, strict=True)
    publish_mode: Literal['small_medium_auto', 'all_auto', 'all_manual'] = DEFAULT_PUBLISH_MODE

    @field_validator('goal', 'rules')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('persona settings cannot be blank')
        return value.strip()

    @model_validator(mode='after')
    def has_changes(self):
        if not self.model_fields_set:
            raise ValueError('at least one persona setting is required')
        return self


class StateSettings(Input):
    stale_after_minutes: int = Field(default=30, ge=1, le=525600, strict=True)


class GoalsSettings(Input):
    default_reminder_minutes: int = Field(default=60, ge=0, le=525600, strict=True)
    overdue_reminders: bool = Field(default=True, strict=True)


class Limits(Input):
    daily_token_limit: int | None = Field(default=None, ge=1, strict=True)
    learning_concurrency: int = Field(default=2, ge=1, le=32, strict=True)


class Lifecycle(Input):
    forget_threshold: int = Field(default=20, ge=1, le=99, strict=True)
    restore_threshold: int = Field(default=35, ge=2, le=100, strict=True)
    feedback_increment: int = Field(default=8, ge=0, le=100, strict=True)
    confirmation_increment: int = Field(default=5, ge=0, le=100, strict=True)
    decay_amount: int = Field(default=1, ge=0, le=100, strict=True)
    auto_delete_enabled: bool = Field(default=True, strict=True)
    auto_delete_days: int = Field(default=180, ge=1, le=36500, strict=True)
    upcoming_delete_days: int = Field(default=14, ge=1, le=36500, strict=True)
    message_retention_days: int = Field(default=30, ge=1, le=36500, strict=True)
    maintenance_time: str = Field(default="03:00", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    abandoned_retry_enabled: bool = Field(default=True, strict=True)
    dependency_penalty: int = Field(default=10, ge=0, le=100, strict=True)


class ConsolidationSettings(Input):
    max_calls: int = Field(default=50, ge=0, le=50, strict=True)
    merge_enabled: bool = Field(default=True, strict=True)
    conflict_enabled: bool = Field(default=True, strict=True)
    dependency_enabled: bool = Field(default=True, strict=True)
    persona_enabled: bool = Field(default=True, strict=True)
    goal_review_enabled: bool = Field(default=True, strict=True)
    maintenance_time: str = Field(default='03:00', strict=True, pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')


def install_settings(app):
    router = APIRouter(prefix='/admin/api')

    @app.exception_handler(ModelKeyRequired)
    async def key_required(request, exc):
        return error('invalid_request', str(exc), fields=[{'field': 'body.api_key', 'message': str(exc)}])

    def snapshot():
        store = app.state.store
        with store.read() as conn:
            lifecycle = lifecycle_settings(conn)
            stored_consolidation = consolidation_settings(conn)
            consolidation = {key:stored_consolidation[key] for key in ConsolidationSettings.model_fields if key != 'maintenance_time'}
            consolidation['maintenance_time'] = lifecycle['maintenance_time']
            persona = persona_settings(conn)
            state = state_settings(conn)
            goals = goal_settings(conn)
            operations = [dict(r) for r in conn.execute('SELECT id,actor,action,created_at FROM admin_operations ORDER BY id DESC LIMIT 30')]
        return {'role': {'name': store.setting('role_name', 'Iris'), 'background': store.setting('background', ''),
                         'timezone': store.setting('timezone', None)},
                'models': app.state.runtime_config.public(),
                'model_source': 'external' if app.state.runtime_config.external_loader else 'local',
                'daily_token_limit': store.setting('daily_token_limit'),
                'learning_concurrency': store.setting('learning_concurrency', 2),
                'consolidation': consolidation, 'persona': persona, 'lifecycle': lifecycle, 'recall_judge': judge_settings(store), 'state': state, 'goals': goals,
                'goal_dedup_judge': goal_judge_settings(store),
                'health': model_health_snapshot(app.state.health), 'presets': PRESETS, 'operations': operations}

    def sync_models():
        configs = app.state.runtime_config.load()
        for kind in MODEL_KINDS:
            app.state.gateway.replace_config(kind, configs.get(kind))

    @router.get('/settings')
    def settings():
        if not app.state.runtime_config.external_loader:
            sync_models()
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

    @router.patch('/settings/lifecycle')
    def save_lifecycle(payload: Lifecycle):
        with app.state.store.write() as conn:
            values = {**lifecycle_settings(conn), **payload.model_dump(exclude_unset=True)}
            # Validate the merged settings: PATCH must not reset unspecified fields.
            if values['restore_threshold'] <= values['forget_threshold']:
                raise ValueError('恢复阈值 H 必须大于遗忘阈值 F')
            conn.execute("INSERT OR REPLACE INTO runtime_settings VALUES('lifecycle',?)", (dumps(values),))
            operation(conn, 'lifecycle_saved', 'settings', 'lifecycle', payload.model_dump(exclude_unset=True))
        app.state.scheduler.wake()
        return snapshot()

    @router.patch('/settings/consolidation')
    def save_consolidation(payload: ConsolidationSettings):
        changes = payload.model_dump(exclude_unset=True)
        with app.state.store.write() as conn:
            values = consolidation_settings(conn)
            values.update({key:value for key,value in changes.items() if key != 'maintenance_time'})
            conn.execute("INSERT OR REPLACE INTO runtime_settings VALUES('consolidation',?)", (dumps(values),))
            if 'maintenance_time' in changes:
                lifecycle = {**lifecycle_settings(conn), 'maintenance_time':changes['maintenance_time']}
                conn.execute("INSERT OR REPLACE INTO runtime_settings VALUES('lifecycle',?)", (dumps(lifecycle),))
            operation(conn, 'consolidation_settings_saved', 'settings', 'consolidation', changes)
        app.state.scheduler.wake()
        return snapshot()

    @router.patch('/settings/role')
    def role(payload: Role):
        with app.state.store.write() as conn:
            update_role(app.state.store, payload.name, payload.background, payload.timezone, _conn=conn)
            audit(conn, 'role_saved', {'fields': ['name', 'background', 'timezone']})
        return snapshot()

    @router.put('/settings/models/{kind}')
    def model(kind: Literal['chat', 'embedding', 'recall_judge', 'goal_dedup_judge', 'image_understanding'], payload: Model):
        runtime = app.state.runtime_config
        if runtime.external_loader:
            return error('external_config', '模型配置来自外部文件，只读', 409)
        saved, key_missing = runtime.editable(kind)
        config = payload.config(saved, kind, key_missing=key_missing)
        runtime.save({kind: config}, repair_secrets=payload.api_key is not None)
        sync_models()
        return snapshot()

    @router.patch('/settings/recall-judge')
    def recall_judge(payload: RecallJudge):
        with app.state.store.write() as conn:
            row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='recall_judge'").fetchone()
            values = {**(json.loads(row[0]) if row else {}), **payload.model_dump(exclude_unset=True)}
            conn.execute("INSERT OR REPLACE INTO runtime_settings VALUES('recall_judge',?)", (dumps(values),))
            audit(conn, 'recall_judge_saved', payload.model_dump(exclude_unset=True))
        return snapshot()

    @router.patch('/settings/goal-dedup-judge')
    def save_goal_judge(payload: GoalDedupJudge):
        with app.state.store.write() as conn:
            row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='goal_dedup_judge'").fetchone()
            changes = payload.model_dump(exclude_unset=True)
            values = {**(json.loads(row[0]) if row else {}), **changes}
            conn.execute("INSERT OR REPLACE INTO runtime_settings VALUES('goal_dedup_judge',?)", (dumps(values),))
            audit(conn, 'goal_dedup_judge_saved', changes)
        app.state.scheduler.wake()
        return snapshot()

    @router.patch('/settings/persona')
    def save_persona_settings(payload: PersonaSettings):
        changes = payload.model_dump(exclude_unset=True)
        with app.state.store.write() as conn:
            for key, value in changes.items():
                conn.execute('INSERT OR REPLACE INTO runtime_settings VALUES(?,?)', ('persona_' + key, dumps(value)))
            operation(conn, 'persona_settings_saved', 'settings', 'persona', {'fields': sorted(changes)})
        return snapshot()

    @router.patch('/settings/state')
    def save_state_settings(payload: StateSettings):
        with app.state.store.write() as conn:
            changes = payload.model_dump(exclude_unset=True)
            values = {**state_settings(conn), **changes}
            conn.execute("INSERT OR REPLACE INTO runtime_settings VALUES('state',?)", (dumps(values),))
            operation(conn, 'state_settings_saved', 'settings', 'state', changes)
        return snapshot()

    @router.patch('/settings/goals')
    def save_goal_settings(payload: GoalsSettings):
        app.state.goals.configure(**payload.model_dump(exclude_unset=True), actor='admin')
        app.state.scheduler.wake()
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
    def retry(kind: Literal['chat', 'embedding', 'recall_judge', 'goal_dedup_judge', 'image_understanding']):
        app.state.gateway.retry_now(kind)
        with app.state.store.write() as conn:
            audit(conn, 'model_retry', {'purpose': kind})
        return snapshot()

    @router.post('/settings/models/{kind}/test')
    def connection(kind: Literal['chat', 'embedding', 'recall_judge', 'goal_dedup_judge', 'image_understanding'], payload: dict):
        runtime = app.state.runtime_config
        current, key_missing = runtime.editable(kind)
        if payload and runtime.external_loader:
            return error('external_config', '外部模型配置只能测试已保存的值', 409)
        if payload:
            # Preserve FastAPI's safe validation envelope (never serialize input).
            from pydantic import ValidationError
            try:
                current = Model.model_validate(payload).config(current, kind, key_missing=key_missing)
            except ValidationError:
                return error('invalid_request', '模型配置无效，请检查输入')
        with app.state.store.write() as conn:
            operation(conn, 'model_test', 'model', kind)
        if not payload and key_missing:
            sync_models()
            return {'ok': False, 'message': '模型密钥不可用，请在设置页重新输入', 'category': 'configuration', 'duration_ms': 0}
        if not current or not current.base_url or not current.model:
            return {'ok': False, 'message': '未配置模型', 'category': 'configuration', 'duration_ms': 0}
        active = app.state.gateway.configs.get(kind) == current
        gateway = app.state.gateway if active else Gateway({kind: current}, app.state.store)
        started = time.monotonic()
        try:
            body = ({'messages': [{'role': 'user', 'content': '只输出 JSON：{"ok":true}'}],
                     'max_tokens': 3000, 'response_format': {'type': 'json_object'}} if kind != 'embedding'
                    else gateway._embedding_payload('Iris 测试连接'))
            if kind == 'image_understanding':
                body = gateway.image_probe_payload()
            result = gateway._call(kind, 'connection_check', body, probe=True, deadline=gateway.monotonic()+CONNECTION_TIMEOUT,
                                   **({'validator': gateway._validate_image_text} if kind == 'image_understanding' else {}))
            valid = (bool((result.get('choices') or [{}])[0].get('message', {}).get('content')) if kind != 'embedding'
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
