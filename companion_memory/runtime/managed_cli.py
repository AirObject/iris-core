"""Deployment commands for one stopped managed volume and compatible builds.

These commands do not construct Provider transports, dispatch models or adopt an
unbound database. Restore transfers the original authority to a separate target;
code upgrade preflight leaves the current database and all new input intact.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import http.client
import json
from pathlib import Path
import platform
import sqlite3
import sys
import tarfile
from typing import cast
from urllib.parse import urlsplit
from contextlib import closing

from companion_memory.configuration.deployment import environment_settings
from companion_memory.persistence.managed_backup import ConsistentBackup
from .managed_bootstrap import ManagedBootstrap
from .managed_resources import ManagedResources, read_record
from .managed_restore import RestoreSwitch


def release() -> dict[str, object]:
    settings = environment_settings()
    bootstrap = ManagedBootstrap(settings)
    base = Path(__file__).resolve().parents[2]
    files = sorted(path for folder in ('companion_memory', 'web/dist') for path in (base / folder).rglob('*')
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc')
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(base).as_posix().encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return {'format': 'MANAGED_BUILD_V1', 'code_digest': digest.hexdigest(),
        'storage_format': 'MANAGED_RUNTIME_V1', 'assembly_digest': bootstrap.assembly_digest,
        'compatible_assembly_digests': [bootstrap.assembly_digest], 'migration_supported': False,
        'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
        'platform': platform.system().lower() + '/' + platform.machine(), 'source_files': len(files)}


def health() -> int:
    settings = environment_settings()
    origin = urlsplit(settings.text('deployment.origin'))
    connection = http.client.HTTPConnection('127.0.0.1', settings.integer('deployment.port'), timeout=3)
    try:
        connection.request('GET', '/health', headers={'Host': origin.netloc})
        response = connection.getresponse()
        data = json.loads(response.read(8193))
        state = data['health']['state']
        print(json.dumps({'state': state, 'business_ready': data['health']['business_ready']}))
        return 0 if response.status == 200 and state in ('BOOTSTRAP', 'AWAITING_REVIEW', 'READY', 'ACTIVATING', 'MAINTENANCE', 'RECOVERING') else 1
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        print('{"state":"UNAVAILABLE"}')
        return 1
    finally:
        connection.close()


def stopped_resources() -> ManagedResources:
    settings = environment_settings()
    root = Path(settings.text('deployment.data_root'))
    # Refuse before constructing any bootstrap identity or creating SQLite.
    if not (root / 'bootstrap/identity.json').is_file() or not (root / 'db/memory.sqlite3').is_file():
        raise ValueError('An existing bound managed instance is required.')
    bootstrap = ManagedBootstrap(settings)
    return ManagedResources(settings, bootstrap.assembly_digest)


def verify_current_database(resources: ManagedResources) -> None:
    """Read the latest WAL state under the exclusive stopped-volume lease."""
    if sqlite3.sqlite_version_info < (3, 51, 3):
        raise ValueError('SQLite runtime is below the admitted version.')
    from companion_memory.persistence._codec import assembly_value
    assembly = ManagedBootstrap(resources.settings).assembly
    expected = assembly_value(assembly.repositories, assembly.commands, assembly_format='MANAGED_RUNTIME_V1')
    database = resources.root / 'db/memory.sqlite3'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        metadata = connection.execute('SELECT database_id, CASE WHEN length(assembly)<=8388608 THEN assembly ELSE NULL END FROM application_metadata WHERE singleton=1').fetchall()
        if metadata != [(resources.database_id, expected)]:
            raise ValueError('The target code cannot interpret this database format; no migration was attempted.')
        if connection.execute('PRAGMA quick_check').fetchone() != ('ok',) or connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('The retained database failed integrity checks.')


async def create_backup(key: str):
    """Make one audited stopped-volume backup without attaching any business host."""
    from companion_memory.persistence import Ready
    from companion_memory.management.managed_application import ManagedApplication
    from .managed_maintenance import ManagedMaintenance
    resources = stopped_resources()
    bootstrap = ManagedBootstrap(resources.settings)
    application = None
    try:
        verify_current_database(resources)
        if type(await bootstrap.open(retained_resources=resources)) is not Ready:
            raise ValueError('The stopped volume could not open its original state.')
        application = ManagedApplication(bootstrap)
        application.maintenance = ManagedMaintenance(application, restore_business=False)
        await application.maintenance.recover()
        result = await application.maintenance.create_local_backup(key)
        if application.maintenance.task is not None:
            # A CLI timeout must not abandon its worker or release its lease.
            result = await application.maintenance.task
        if type(result) is not dict:
            raise ValueError('Backup request was not confirmed.')
        return {key: result[key] for key in ('state', 'backup_id', 'startup_sends') if key in result}
    finally:
        if application is not None:
            bootstrap = application.bootstrap
        while not await bootstrap.close():
            await asyncio.sleep(0.1)
        resources.close()


def main() -> None:
    parser = argparse.ArgumentParser(description='受控本地安装、备份与兼容版本检查；不会发送模型请求。')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('release', help='输出当前代码及兼容格式声明')
    commands.add_parser('health', help='只读检查同一服务健康')
    commands.add_parser('preflight', help='停止服务后核对当前卷与此构建兼容；不迁移、不改写业务数据')
    backup = commands.add_parser('create-backup', help='停止服务后按原操作键创建一致备份；重复执行确认原结果')
    backup.add_argument('operation_key')
    archive = commands.add_parser('import-backup', help='停止服务后从完整下载归档导入原实例备份；不覆盖现有数据')
    archive.add_argument('archive', type=Path)
    archive.add_argument('operation_key')
    verify = commands.add_parser('verify-backup', help='停止服务后完整核验已完成备份')
    verify.add_argument('backup_id')
    prepare = commands.add_parser('restore-prepare', help='停止服务后恢复到不存在的隔离目录，保留原库')
    prepare.add_argument('backup_id')
    prepare.add_argument('target', type=Path)
    activate = commands.add_parser('restore-activate', help='按原切换身份退役源卷并激活已验证目标；之后将目标挂载到原 /data 路径')
    activate.add_argument('target', type=Path)
    activate.add_argument('switch_id')
    audit = commands.add_parser('audit-issue', help='停止服务后按私有范围文件签发独立只读审计能力；只输出私有文件路径')
    audit.add_argument('scope_file', type=Path)
    audit.add_argument('output', type=Path)
    audit.add_argument('lifetime_seconds', type=int)
    args = parser.parse_args()
    try:
        if args.command == 'release':
            value = release()
        elif args.command == 'health':
            raise SystemExit(health())
        elif args.command == 'audit-issue':
            from companion_memory.management.audit_provisioning import issue_audit_grant
            resources = stopped_resources()
            try:
                verify_current_database(resources)
                value = issue_audit_grant(resources, args.scope_file, args.output, args.lifetime_seconds)
            finally:
                resources.close()
        elif args.command == 'create-backup':
            from companion_memory.persistence.schema import valid_identifier
            if not valid_identifier(args.operation_key):
                raise ValueError('Invalid operation identity.')
            value = asyncio.run(create_backup(args.operation_key))
            if type(value) is not dict or value.get('state') != 'COMPLETE':
                raise ValueError('Backup is not confirmed complete; retain its original operation key.')
        elif args.command == 'import-backup':
            from companion_memory.persistence.managed_archive import ManagedBackupArchive
            resources = stopped_resources()
            try:
                verify_current_database(resources)
                value = ManagedBackupArchive(resources).import_archive(args.archive, args.operation_key)
            finally:
                resources.close()
        elif args.command in ('preflight', 'verify-backup'):
            resources = stopped_resources()
            try:
                if args.command == 'preflight':
                    verify_current_database(resources)
                    value = {'compatible': True, 'build': release(), 'database_id': resources.database_id,
                        'instance_id': resources.instance_id, 'startup_sends': 0,
                        'authority': read_record(resources.root / 'bootstrap/identity.json')['state']}
                else:
                    from companion_memory.persistence.schema import valid_identifier
                    if not valid_identifier(args.backup_id) or '/' in args.backup_id or args.backup_id in ('.', '..'):
                        raise ValueError('Invalid backup identity.')
                    manifest = ConsistentBackup(resources, lambda: True).verify(resources.root / 'backups' / args.backup_id)
                    value = {'verified': True, 'backup_id': manifest['backup_id'], 'file_count': len(cast(list, manifest['files'])), 'startup_sends': 0}
            finally:
                resources.close()
        else:
            settings = environment_settings()
            root = Path(settings.text('deployment.data_root'))
            if not (root / 'bootstrap/identity.json').is_file():
                raise ValueError('An existing bound source is required.')
            bootstrap = ManagedBootstrap(settings)
            switch = RestoreSwitch(settings, bootstrap.assembly_digest)
            try:
                value = (switch.prepare(args.backup_id, args.target) if args.command == 'restore-prepare'
                    else switch.activate(args.target, args.switch_id))
            finally:
                switch.close()
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError) as error:
        print(json.dumps({'state': 'REFUSED', 'reason': str(error), 'startup_sends': 0}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
