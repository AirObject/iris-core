"""Adapt native media ownership to finite Provider byte and image inputs.

The exact owner check stays here; the consumer receives no paths, SQL, settings
or media service. Every read still verifies the original PROCESSING reference.
"""
from hashlib import sha256
from typing import cast
from companion_memory.persistence import UnitOfWork
from companion_memory.persistence.schema import InvalidValue
from companion_memory.provider.media_input import (
    StoredMediaSource, MediaBytes, MediaRead, MediaInputError,
    ImageContent, ImageInput, ImageRequestBinding,
)
from .service import MediaService, MediaError
from .processing_bytes import ProcessingBytes
from .image_validation import CheckedImage
from .daily_image import DailyImageLease, DailyImages


def stored_media_source(media: MediaService) -> StoredMediaSource:
    """Bind live ownership, bounded capacity and original read completion checks."""
    if type(media) is not MediaService:
        raise TypeError('The native media owner is required.')

    async def read(work_id: object, occurrence_id: object) -> MediaRead:
        original = await media.read_processing(work_id, occurrence_id)
        value = original.result
        if type(value) is MediaError:
            result = MediaInputError(value.code, value.operation, value.field, value.reason, value.cleanup_pending)
        elif type(value) is ProcessingBytes:
            result = MediaBytes(value.content, value.artifact_id, value.modality)
        else:
            raise InvalidValue()
        return MediaRead(result, original.completion)

    source = object.__new__(StoredMediaSource)
    object.__setattr__(source, 'matches_provider', media.matches_provider)
    object.__setattr__(source, 'read_processing', read)
    object.__setattr__(source, 'capacity', lambda: media.settings.integer('media.processing_concurrency'))
    return source


def media_input_error(error: MediaInputError) -> MediaError:
    """Translate a consumer failure back to the unchanged media-facing result."""
    if type(error) is not MediaInputError:
        raise InvalidValue()
    return MediaError(error.code, error.operation, error.field, error.reason, error.cleanup_pending)


def image_content(image: CheckedImage) -> ImageContent:
    """Project decoded evidence without copying or re-encoding the original bytes."""
    if type(image) is not CheckedImage:
        raise InvalidValue()
    return ImageContent(image.data, image.sha256, image.format, image.width, image.height)


def image_input(lease: DailyImageLease) -> ImageInput:
    """Adapt an actual retained lease; keep media rows and release authority private."""
    if type(lease) is not DailyImageLease or type(lease.owner) is not DailyImages:
        raise InvalidValue()
    owner = lease.owner
    owner.verify(lease)
    work = lease.work
    binding = ImageRequestBinding(
        cast(str, work['work_id']), cast(str, work['original_operation_key']),
        cast(str, work['prompt_revision']), cast(str, work['profile_id']),
        cast(int, work['deadline_at_us']), cast(str, work['entry_id']),
        cast(str, work['occurrence_id']), cast(str, work['blob_id']),
        cast(int, work['generation']),
        sha256(cast(str, work['original_request_descriptor']).encode()).hexdigest(),
    )

    def verify(uow: UnitOfWork | None) -> ImageContent:
        return image_content(owner.verify(lease, uow))

    value = object.__new__(ImageInput)
    for name, item in (
        ('binding', binding), ('artifact_id', lease.artifact_id), ('deadline', lease.deadline),
        ('matches_provider', owner.matches_provider), ('verify', verify),
        ('reader_active', lambda: owner.reader_active(lease)),
    ):
        object.__setattr__(value, name, item)
    return value
