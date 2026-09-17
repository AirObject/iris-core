"""Pure cross-layer limits for complete media, source, candidate and history work.

The functions inspect configuration-issued values and path text only. Physical
file identity and independent ownership are validated by resource initialization.
"""
from __future__ import annotations
from typing import TYPE_CHECKING, cast
from types import MappingProxyType
from pathlib import PurePosixPath
from .snapshots import EffectiveSnapshot, PresentValue
if TYPE_CHECKING:
    from .content_resolution import ContentSettingsSnapshot, ContentPlatformSnapshot


def validate_content_relationships(foundation: EffectiveSnapshot, runtime: ContentSettingsSnapshot,
                                   content: ContentSettingsSnapshot, platforms: tuple[ContentPlatformSnapshot, ...],
                                   directories: object, *, text_only: bool = False, embedding_only: bool = False, daily_network: bool = False,
                                   dream_network: bool = False) -> str | None:
    """Return a fixed first failure, never a submitted key or resource path."""
    f = {e.definition.key: e.state.value for e in foundation.list_entries() if type(e.state) is PresentValue}
    if dream_network and not daily_network:
        return 'BUDGET_INVALID'
    n, r = content.integer, runtime.integer
    if not n('memory.forget_below') < n('memory.restore_at'):
        return 'BUDGET_INVALID'
    if n('cognition.candidate_item_limit') * n('cognition.candidate_item_max_bytes') + (8192 if daily_network else 4096) > n('cognition.candidate_max_bytes'):
        return 'CAPACITY_INSUFFICIENT'
    if n('audit.history_items_per_operation') < n('cognition.candidate_item_limit') or n('audit.history_item_max_bytes') < n('memory.current_max_bytes') + 4096:
        return 'CAPACITY_INSUFFICIENT'
    if n('media.file_worker_capacity') < sum(n('media.' + key) for key in ('upload_concurrency', 'read_concurrency', 'processing_concurrency')):
        return 'BUDGET_INVALID'
    if not daily_network and n('media.processing_concurrency') + r('runtime.max_active_entries') > cast(int, f['provider.max_in_flight']):
        return 'BUDGET_INVALID'
    if max(n('media.upload_chunk_bytes'), n('media.read_chunk_bytes')) > n('media.blob_max_bytes'):
        return 'BUDGET_INVALID'
    if n('media.processing_suspect_after_ms') <= cast(int, f['provider.request_timeout_ms']) or n('media.occurrence_total_timeout_ms') < cast(int, f['provider.request_timeout_ms']):
        return 'BUDGET_INVALID'
    for platform in platforms:
        members = sum(platform.count(k) for k in ('history_context_count', 'target_count', 'recent_context_count'))
        required = members * n('media.event_occurrence_limit') * n('media.occurrence_total_timeout_ms') + 4 * cast(int, f['storage.operation_timeout_ms'])
        if n('media.preparation_total_timeout_ms') < required:
            return 'BUDGET_INVALID'
    profiles = cast(tuple[MappingProxyType[str, object], ...], f['provider.profiles'])
    roles = cast(MappingProxyType[str, tuple[str, ...]], f['provider.role_profiles'])
    media = tuple(p for p in profiles if p['profile_id'] in roles.get('MEDIA', ()) and p['capability'] == 'MEDIA_UNDERSTANDING')
    if daily_network:
        expected_roles={'LEARNING','MEDIA','GOAL_DEDUP','PERSONA','EMBEDDING_DOCUMENT','EMBEDDING_QUERY'}
        if dream_network:
            expected_roles.update(('DREAM_REVIEW','PERSONA_DREAM','PERSONA_REVIEW'))
        if text_only or embedding_only or len(media)!=1 or set(roles)!=expected_roles or n('media.processing_concurrency')!=1 or f['provider.max_in_flight']!=1:
            return 'BUDGET_INVALID'
    elif embedding_only:
        if text_only or media or set(roles) != {'EMBEDDING_DOCUMENT', 'EMBEDDING_QUERY'} or n('media.processing_concurrency') != 0:
            return 'BUDGET_INVALID'
    elif text_only:
        if media or set(roles) != {'LEARNING', 'PERSONA'} or n('media.processing_concurrency') != 0:
            return 'BUDGET_INVALID'
    elif not media or any(n('media.blob_max_bytes') > cast(int, p['max_input_units']) for p in media):
        return 'BUDGET_INVALID'
    command_limit = cast(int, f['storage.command_max_bytes'])
    if min(command_limit, cast(int, f['storage.receipt_max_bytes'])) < 57344 or command_limit < 933888:
        return 'CAPACITY_INSUFFICIENT'
    root, staging = content.value('media.root_directory'), content.value('media.staging_directory')
    if type(root) is not str or type(staging) is not str:
        return 'BUDGET_INVALID'
    for path in (root, staging):
        if not path.startswith('/') or '\x00' in path or any(part in ('.', '..') for part in path.split('/')) or path == '/':
            return 'BUDGET_INVALID'
    if root == staging or not PurePosixPath(staging).is_relative_to(PurePosixPath(root)):
        return 'BUDGET_INVALID'
    if directories is not None:
        if type(directories) is not dict or any(type(k) is not str for k in directories):
            return 'BUDGET_INVALID'
        media_paths = directories.get('media')
        if (type(media_paths) is not list and type(media_paths) is not tuple) or root not in media_paths or staging not in media_paths:
            return 'BUDGET_INVALID'
        for group, paths in directories.items():
            if type(paths) not in (tuple, list) or any(type(path) is not str for path in paths):
                return 'BUDGET_INVALID'
            if group == 'media':
                continue
            for path in paths:
                if PurePosixPath(root).is_relative_to(path) or PurePosixPath(path).is_relative_to(root):
                    return 'BUDGET_INVALID'
    return None
