"""Deployment options, database settings and atomic private credential files."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import tomllib
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .auth import audit
from .db import dumps
from .models import ModelConfig
from .process_lock import StoreLease

KINDS = ('chat', 'embedding')


def deployment(*, config=None, data_dir=None, host=None, port=None):
    path = Path(config or 'iris.toml').resolve()
    try:
        values = tomllib.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        if config:
            raise ValueError('找不到部署配置文件') from None
        values = {}
    except (ValueError, OSError):
        raise ValueError('部署配置文件无效') from None
    if set(values)-{'data_dir', 'host', 'port'}:
        raise ValueError('iris.toml 只接受 data_dir、host、port')
    directory = data_dir or os.environ.get('IRIS_DATA_DIR')
    if directory is None:
        directory = path.parent / str(values.get('data_dir', 'data'))
    return {'data_dir': Path(directory).resolve(), 'host': host or os.environ.get('IRIS_HOST') or values.get('host', '127.0.0.1'),
            'port': int(port if port is not None else os.environ.get('IRIS_PORT', values.get('port', 8080)))}


class RuntimeConfig:
    def __init__(self, store, *, data_dir=None, external_loader=None):
        self.store = store
        self.data_dir = Path(data_dir or store.path.parent)
        self.path = self.data_dir / 'secrets.json'
        self.external_loader = external_loader
        self._lock = threading.RLock()

    @contextmanager
    def locked(self):
        with self._lock:
            try:
                lease = StoreLease(self.data_dir / '.configuration')
                lease.__enter__()
            except (OSError, ValueError):
                raise ValueError('配置正在更新，请稍后重试') from None
            try:
                yield
            finally:
                lease.__exit__()

    def _secrets(self):
        try:
            if self.path.is_symlink():
                raise ValueError('密钥文件不能是符号链接')
            if not self.path.exists():
                return {}
            if os.name != 'nt':
                self.path.chmod(0o600)
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('format_version') != 1 or not isinstance(data.get('keys'), dict):
                raise ValueError()
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in data['keys'].items()):
                raise ValueError()
            return data['keys']
        except (OSError, ValueError, AttributeError):
            raise ValueError('无法读取 secrets.json，请检查格式和权限') from None

    def _write_secrets(self, keys):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.secrets-', dir=self.data_dir)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                if os.name != 'nt':
                    os.fchmod(file.fileno(), 0o600)
                json.dump({'format_version': 1, 'keys': keys}, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _load_local(self):
        rows = self.store.setting('models', {})
        keys = self._secrets()
        result = {}
        for kind in KINDS:
            row = rows.get(kind)
            if row:
                reference = row.get('api_key_ref')
                if reference and reference not in keys:
                    raise ValueError('模型密钥引用缺失，请重新导入或保存模型配置')
                result[kind] = ModelConfig(row['base_url'], keys.get(reference, ''), row['model'], row.get('dimensions'), row.get('reasoning_effort'))
        return result

    def load(self):
        if self.external_loader:
            try:
                return self.external_loader()
            except (OSError, ValueError):
                raise ValueError('外部模型配置暂不可用，请检查原文件') from None
        with self.locked():
            return self._load_local()

    def save(self, updates, *, actor='admin'):
        if self.external_loader:
            raise ValueError('模型配置来自外部文件，只读')
        with self.locked():
            old = self.store.setting('models', {})
            keys = self._secrets()
            # Keep current references until the database commit. A crash cannot pair
            # a newly imported key with the previous provider URL.
            keep = {row['api_key_ref'] for row in old.values() if row.get('api_key_ref')}
            keys = {key: value for key, value in keys.items() if key in keep}
            rows = dict(old)
            for kind, config in updates.items():
                if kind not in KINDS:
                    raise ValueError('未知模型用途')
                if config is None:
                    rows.pop(kind, None)
                    continue
                row = asdict(config)
                key = row.pop('api_key')
                if key:
                    reference = secrets.token_hex(16)
                    keys[reference] = key
                    row['api_key_ref'] = reference
                rows[kind] = row
            self._write_secrets(keys)
            with self.store.write() as conn:
                conn.execute('INSERT OR REPLACE INTO runtime_settings VALUES(?,?)', ('models', dumps(rows)))
                audit(conn, 'models_saved', {'purposes': sorted(updates)}, actor=actor)

    def public(self):
        configs = self.load()
        return {kind: {'enabled': bool(configs.get(kind) and configs[kind].base_url and configs[kind].model),
                       'base_url': configs[kind].base_url if configs.get(kind) else '',
                       'model': configs[kind].model if configs.get(kind) else '',
                       'dimensions': configs[kind].dimensions if configs.get(kind) else None,
                       'reasoning_effort': configs[kind].reasoning_effort if configs.get(kind) else None,
                       'key_set': bool(configs.get(kind) and configs[kind].api_key)} for kind in KINDS}
