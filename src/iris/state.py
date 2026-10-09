"""One host-owned current activity and its append-only report history.

No learning, memory, persona or goal writes belong here. Reads use their caller's
snapshot; each report and its current value commit in one short transaction.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .db import dumps
from .model_health import utc_now

MAX_REPORT_BYTES = 32768
DetailKey = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r'\S')]
DetailValue = (Annotated[str, Field(max_length=1000)] | Annotated[bool, Field(strict=True)]
               | Annotated[int, Field(strict=True)]
               | Annotated[float, Field(strict=True, allow_inf_nan=False)] | None)


class StateError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


class SourceReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    host: str | None = Field(default=None, min_length=1, max_length=100)
    entry_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator('host', 'entry_id')
    @classmethod
    def source_label(cls, value):
        if value is not None:
            if not value.strip():
                raise ValueError('来源标识不能为空白')
            return value.strip()
        return value


class PatchReport(SourceReport):
    details: dict[DetailKey, DetailValue] = Field(default_factory=dict, max_length=32)
    mood: str | None = Field(default=None, max_length=200)


class PutReport(PatchReport):
    activity: str = Field(min_length=1, max_length=200)
    started_at: str = Field(default=None, max_length=100)

    @field_validator('activity')
    @classmethod
    def activity_label(cls, value):
        if not value.strip():
            raise ValueError('活动不能为空白')
        return value.strip()

    @field_validator('started_at')
    @classmethod
    def start_time(cls, value):
        try:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                raise ValueError()
            return stamp.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            raise ValueError('开始时间须为带时区的 ISO 时间') from None


def state_settings(conn):
    row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='state'").fetchone()
    return {'stale_after_minutes': 30, **(json.loads(row[0]) if row else {})}


def role_zone(conn):
    row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='timezone'").fetchone()
    return ZoneInfo(json.loads(row[0]) if row else 'Asia/Shanghai')


def local_time(value, zone):
    return datetime.fromisoformat(value).astimezone(zone).isoformat() if value else None


def current_state(conn, *, current=None):
    row = conn.execute('SELECT * FROM current_state WHERE id=1').fetchone()
    if row is None:
        return {}
    current = (current or utc_now()).astimezone(timezone.utc)
    result = dict(row)
    result.pop('id')
    result['details'] = json.loads(result.pop('details_json'))
    result.update(state_settings(conn))
    result['duration_seconds'] = max(0.0, (current - datetime.fromisoformat(row['started_at'])).total_seconds())
    result['possibly_stale'] = ((current - datetime.fromisoformat(row['updated_at'])).total_seconds()
                                > result['stale_after_minutes'] * 60)
    zone = role_zone(conn)
    for key in ('activity_updated_at', 'mood_updated_at', 'started_at', 'updated_at'):
        result[key] = local_time(result[key], zone)
    for detail in result['details'].values():
        detail['updated_at'] = local_time(detail['updated_at'], zone)
    return result


def report_history(conn, *, limit=30, offset=0):
    zone = role_zone(conn)
    items = []
    for row in conn.execute('SELECT * FROM state_reports ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset)):
        item = dict(row)
        item['reported_at'] = local_time(item['reported_at'], zone)
        item['reported'] = json.loads(item.pop('reported_json'))
        item['changes'] = json.loads(item.pop('changes_json'))
        if 'started_at' in item['reported']:
            item['reported']['started_at'] = local_time(item['reported']['started_at'], zone)
        if 'started_at' in item['changes']:
            item['changes']['started_at'] = {k: local_time(v, zone) for k, v in item['changes']['started_at'].items()}
        items.append(item)
    return {'items': items, 'total': conn.execute('SELECT COUNT(*) FROM state_reports').fetchone()[0],
            'limit': limit, 'offset': offset}


def _changes(before, after):
    """Semantic changes, separate from explicit reports of unchanged values."""
    result = {}
    for key in ('activity', 'mood', 'started_at', 'start_time_basis'):
        old, new = before.get(key), after.get(key)
        if old != new:
            result[key] = {'before': old, 'after': new}
    old_details, new_details = before.get('details', {}), after.get('details', {})
    details = {}
    for key in sorted(old_details.keys() | new_details.keys()):
        old, new = old_details.get(key, {}).get('value'), new_details.get(key, {}).get('value')
        # JSON true and 1 are distinct values even though Python equates them.
        if dumps(old) != dumps(new):
            details[key] = {'before': old, 'after': new}
    if details:
        result['details'] = details
    return result


class CurrentState:
    def __init__(self, store, *, clock=utc_now):
        self.store, self.clock = store, clock

    def get(self):
        with self.store.read() as conn:
            return current_state(conn, current=self.clock())

    def put(self, **fields):
        return self._report('PUT', PutReport.model_validate(fields))

    def patch(self, **fields):
        return self._report('PATCH', PatchReport.model_validate(fields))

    def delete(self, **fields):
        return self._report('DELETE', SourceReport.model_validate(fields))

    def _report(self, method, payload):
        fields = payload.model_dump(exclude_unset=True)
        if len(dumps(fields).encode('utf-8')) > MAX_REPORT_BYTES:
            raise StateError('body', '状态报告超过 32KB UTF-8 上限')
        host, entry_id = fields.pop('host', None), fields.pop('entry_id', None)
        with self.store.write() as conn:
            # Take the receipt time AFTER obtaining the writer lock. Report IDs
            # define serialization order, even if the system clock moves back.
            current = self.clock().astimezone(timezone.utc)
            stamp = current.isoformat()
            row = conn.execute('SELECT * FROM current_state WHERE id=1').fetchone()
            before = dict(row) if row else {}
            if before:
                before['details'] = json.loads(before.pop('details_json'))
            if method == 'PATCH' and not before:
                raise StateError('activity', '当前没有活动，请先用 PUT 报告活动')
            if 'started_at' in fields and datetime.fromisoformat(fields['started_at']) > current:
                raise StateError('started_at', '开始时间不能晚于本次报告时间')
            if method == 'DELETE':
                after, action = {}, 'end'
                conn.execute('DELETE FROM current_state WHERE id=1')
            else:
                replacement = method == 'PUT' and fields['activity'] != before.get('activity')
                if replacement:
                    after = {'activity': fields['activity'], 'activity_updated_at': stamp, 'details': {},
                             'mood': before.get('mood'), 'mood_updated_at': before.get('mood_updated_at'),
                             'started_at': stamp,
                             'start_time_basis': 'first_report'}
                else:
                    after = {**before, 'details': dict(before['details'])}
                if method == 'PUT':
                    after['activity_updated_at'] = stamp
                if 'started_at' in fields:
                    after.update(started_at=fields['started_at'], start_time_basis='host')
                for key, value in fields.get('details', {}).items():
                    if value is None:
                        after['details'].pop(key, None)
                    else:
                        after['details'][key] = {'value': value, 'updated_at': stamp}
                values = {k: v['value'] for k, v in after['details'].items()}
                if len(values) > 32 or len(dumps(values).encode('utf-8')) > MAX_REPORT_BYTES:
                    raise StateError('details', '合并后的细节最多 32 项、32KB UTF-8')
                if 'mood' in fields:
                    after.update(mood=fields['mood'], mood_updated_at=stamp)
                after.update(updated_at=stamp, host=host, entry_id=entry_id)
                action = ('start' if not before else 'replace') if replacement else (
                    'update' if _changes(before, after) else 'heartbeat')
                conn.execute('''INSERT INTO current_state(id,activity,activity_updated_at,details_json,mood,
                    mood_updated_at,started_at,start_time_basis,updated_at,host,entry_id)
                    VALUES(1,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    activity=excluded.activity,activity_updated_at=excluded.activity_updated_at,
                    details_json=excluded.details_json,mood=excluded.mood,mood_updated_at=excluded.mood_updated_at,
                    started_at=excluded.started_at,start_time_basis=excluded.start_time_basis,
                    updated_at=excluded.updated_at,host=excluded.host,entry_id=excluded.entry_id''',
                    (after['activity'], after['activity_updated_at'], dumps(after['details']), after['mood'],
                     after['mood_updated_at'], after['started_at'], after['start_time_basis'], stamp, host, entry_id))
            conn.execute('''INSERT INTO state_reports(host,entry_id,reported_at,method,action,reported_json,changes_json)
                VALUES(?,?,?,?,?,?,?)''', (host, entry_id, stamp, method, action, dumps(fields), dumps(_changes(before, after))))
            return current_state(conn, current=current)
