"""Backup and restore ownership facts, distinct from application data snapshots.

Only COMPLETE backups may be offered for restore. A restore is verified on an
isolated volume and cannot acquire sending authority from copied state alone.
"""
from . import Field, RecordSchema
from .daily_records import BASE, DailyTable, daily_catalog, ID, UINT, DIGEST, enum

BACKUP = RecordSchema(BASE + (
    Field('state', enum('REQUESTED', 'QUIESCING', 'COPYING', 'VERIFYING', 'COMPLETE', 'FAILED')),
    Field('manifest_digest', DIGEST, nullable=True), Field('total_bytes', UINT),
    Field('file_count', UINT), Field('source_authority', ID), Field('failure', ID, nullable=True),
    Field('request_key', ID), Field('request_actor', ID),
))
RESTORE = RecordSchema(BASE + (
    Field('backup_id', ID), Field('manifest_digest', DIGEST),
    Field('state', enum('VERIFYING', 'VERIFIED', 'SWITCHING', 'ACTIVE', 'FAILED')),
    Field('source_authority', ID), Field('target_authority', ID), Field('target_binding', DIGEST),
))
TABLES = (DailyTable('backups', (BACKUP,), 4096, True), DailyTable('restores', (RESTORE,), 4096, True))


def backup_catalog():
    return daily_catalog('backup', 1, TABLES)
