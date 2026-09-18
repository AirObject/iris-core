"""Trusted logical recipient administration through the management owner.

Offline and disabled recipients remain legal goal destinations. An explicit
token ACL restricts host writes; only one eligible candidate is a default.
"""
from __future__ import annotations

import json
from typing import cast
from companion_memory.persistence.owned_statements import OwnerFailure
from .identity import IdentityAuthority, Principal, digest
from .communication_records import EVENT_TYPES


class NotificationRoutes:
    def __init__(self, identity: IdentityAuthority):
        self.identity = identity

    async def candidates(self, host_id: str, entry_id: str, principal: Principal | None = None) -> tuple[str, ...]:
        """Return only registered destinations; enabled and online are separate facts."""
        if not self.identity.communication_format:
            return ()
        rows = await self.all_rows()
        return tuple(cast(str, row['object_id']) for row in rows
            if row['host_id'] == host_id and entry_id in cast(tuple[str, ...], row['entries'])
            and (principal is None or row['object_id'] in principal.route_ids))

    async def for_entry(self, entry_id: str) -> tuple[str, ...]:
        """Supply registered routes to trusted internal work before it freezes input."""
        rows = await self.all_rows()
        return tuple(cast(str, row['object_id']) for row in rows if entry_id in cast(tuple[str, ...], row['entries']))

    async def all_ids(self) -> tuple[str, ...]:
        """Bounded scheduler capability, independent of online consumers."""
        return tuple(cast(str, row['object_id']) for row in await self.all_rows())

    async def all_rows(self):
        """Read the declared sixteen-route capacity through bounded native pages."""
        rows = []
        after = ''
        while len(rows) < 16:
            page = await self.identity.rows.page('notification_routes', after)
            if not page: break
            rows.extend(page)
            after = cast(str, page[-1]['object_id'])
        return tuple(rows)

    async def create(self, key: str, route_id: str, host_id: str, entries: tuple[str, ...],
                     events: tuple[str, ...]):
        """Create disabled, immutable ownership with the original input and audit."""
        from companion_memory.persistence.schema import valid_identifier
        if (not self.identity.communication_format or not valid_identifier(route_id) or not valid_identifier(host_id)
                or not 1 <= len(entries) <= 64 or len(set(entries)) != len(entries)
                or any(not valid_identifier(entry) for entry in entries) or not events
                or len(set(events)) != len(events) or not set(events) <= set(EVENT_TYPES)):
            raise OwnerFailure('INVALID_INPUT', 'route', 'INVALID_SHAPE')
        request = digest(json.dumps([route_id, host_id, entries, events], separators=(',', ':')))
        prior = await self.identity.confirm_request('create_notification_route', key, request)
        if prior is not None:
            return prior
        verify = self.identity.verify_binding
        if verify is None:
            raise OwnerFailure('INVALID_STATE', 'binding', 'NOT_READY')
        for entry in entries:
            if not await verify(entry, host_id):
                raise OwnerFailure('ACCESS_DENIED', 'binding', 'BINDING_MISMATCH')
        return await self.identity.write('create_notification_route', key, self.identity.row(route_id,
            {'host_id': host_id, 'entries': entries, 'event_types': events, 'enabled': False}), None, request)

    async def enable(self, key: str, route_id: str, expected: int, enabled: bool):
        """Change only enablement; old targets, aliases and attempts remain intact."""
        request = digest(json.dumps([route_id, expected, enabled], separators=(',', ':')))
        prior = await self.identity.confirm_request('update_notification_route', key, request)
        if prior is not None:
            return prior
        old = await self.identity.rows.read('notification_routes', route_id)
        if old is None or old['revision'] != expected:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        return await self.identity.write('update_notification_route', key,
            self.identity.row(route_id, {'enabled': enabled}, old), expected, request)
