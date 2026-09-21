"""Native bridge from the actual media owner to finite Provider byte authority.

Provider receives verified immutable bytes and the original protected artifact,
never a path. Registration is separate from the legacy synthetic source. Release
keeps the native capability while an original consumer still owns execution.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .media_input import StoredMediaSource
    from .resources import AuthorizedMedia
    from .service import ProviderService
    from .media_input import MediaInputError


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class StoredMediaAuthority:
    """Trusted media owner binding for one database, scope and result owner."""
    _provider: ProviderService
    _media: StoredMediaSource
    _scope: str
    _owner: str

    def __init__(self): raise TypeError('Stored byte authority requires native trusted assembly.')

    async def authorize_stored_media(self, work_id: object, occurrence_id: object) -> StoredMediaAuthorized | MediaInputError:
        """Read and verify protected bytes; no caller-supplied hash grants access."""
        from .service import ProviderService
        from .media_input import MediaInputError
        try: provider = object.__getattribute__(self, '_provider')
        except AttributeError: provider = None
        if type(self) is not StoredMediaAuthority or type(provider) is not ProviderService or provider._stored_authorities.get(id(self)) is not self:
            return MediaInputError('ACCESS_DENIED', 'authorize_stored_media', 'capability', 'BINDING_MISMATCH')
        return await provider._authorize_stored(self, work_id, occurrence_id)

    def release_media_authorization(self, media: object) -> bool:
        """Idempotently release only this binding's ended consumers and bytes."""
        from .service import ProviderService
        try: provider = object.__getattribute__(self, '_provider')
        except AttributeError: return False
        if type(self) is not StoredMediaAuthority or type(provider) is not ProviderService or provider._stored_authorities.get(id(self)) is not self:
            return False
        from .resources import AuthorizedMedia
        if type(media) is not AuthorizedMedia: return False
        existing = provider._stored_media.get(media)
        if existing is None: return media not in provider._media
        if existing[0] is not self or media in provider._lookup_consumers.values() or any(job.media_authorization is media for job in provider._jobs): return False
        provider._stored_media.pop(media); provider._media.pop(media, None)
        # The former caller may retain an opaque carrier. Clear its only strong
        # byte reference once native registration and all consumers have ended.
        object.__setattr__(media, '_content', b'')
        return True


@dataclass(frozen=True, slots=True)
class StoredMediaAuthorized:
    """A bounded native capability, separate from serializable ledger results."""
    media: AuthorizedMedia
    artifact_id: str
