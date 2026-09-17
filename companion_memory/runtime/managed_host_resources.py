"""Production resource construction without trial datasets or implicit sends."""
from __future__ import annotations
from collections.abc import Callable
import time
from typing import cast

from companion_memory.configuration.managed_resolution import ManagedConfigurationCandidate
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.media.service import MediaResources
from companion_memory.persistence import DatabaseResources
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.credentials import CredentialResolver, CredentialLease, Available, CredentialUnavailable
from companion_memory.provider.values import Record, as_record
from companion_memory.configuration import PresentValue
from .daily_host import DailyHostResources
from .managed_resources import ManagedResources


def host_resources(resources: ManagedResources, candidate: ManagedConfigurationCandidate,
                   configuration_key: str, role_name: str, send_authorized: Callable[[str], bool]) -> DailyHostResources:
    """Bind declared references; credential bytes are read only by Provider transport."""
    wire = cast(tuple[Record, ...], candidate.text.record('provider.transport')['roles'])
    embedding = candidate.text.record('provider.embedding_transport')
    profiles = next(entry.state.value for entry in candidate.foundation.list_entries()
        if entry.definition.key == 'provider.profiles' and type(entry.state) is PresentValue)
    embedding_account = next(as_record(profile)['account_id'] for profile in cast(tuple[Record, ...], profiles)
        if as_record(profile)['material_role'] == 'EMBEDDING_DOCUMENT')
    declared = {(cast(str, record['secret_ref']), cast(str, record['secret_revision']), cast(str, record['account_ref'])) for record in wire}
    declared.add((cast(str, embedding['secret_ref']), cast(str, embedding['secret_revision']), cast(str, embedding_account)))
    def resolve(reference: str, revision: str, account: str):
        if (reference, revision, account) not in declared:
            return CredentialUnavailable('UNAVAILABLE')
        try:
            return Available(CredentialLease(resources.read_secret(reference + '__' + revision)))
        except (OSError, ValueError):
            return CredentialUnavailable('UNAVAILABLE')
    resolver = CredentialResolver(resolve)
    transports = {cast(str, setting['role']): (ChatTransport.dream if setting['role'] in
        ('DREAM_REVIEW', 'PERSONA_DREAM', 'PERSONA_REVIEW') else ChatTransport.daily)(setting, resolver, time.monotonic)
        for setting in wire}
    embedding_transport = ChatTransport.for_embedding(cast(Record, embedding), resolver, cast(str, embedding_account), time.monotonic)
    root_id = resources.database_id + ':media'
    from companion_memory.media.restore_resources import restored_media_resources
    restoration = restored_media_resources(resources)
    return DailyHostResources(resources.root, DatabaseResources(resources.database_id, resources.retained_identity_check),
        MediaResources(root_id, lambda actual, database, root: actual == root_id and database == resources.database_id
            and root == str(resources.root / 'blobs') and resources.retained_identity_check(database, str(resources.root / 'db/memory.sqlite3')),
            restoration),
        resources.instance_id, configuration_key, dict(resources.protected_directories()),
        InitialSelfBinding('administrator', 'self', role_name, 'ACTUAL_INPUT'), None, None,
        embedding_transport, transports, send_authorized,
        birth_resource_identity=(cast(int, resources.identity['birth_device']), cast(int, resources.identity['birth_inode'])),
        version_transports=lambda version: host_resources(resources, cast(ManagedConfigurationCandidate, version),
            configuration_key, role_name, send_authorized).transports)
