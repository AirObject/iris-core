"""Decode bounded original PNG/JPEG bytes before image-understanding admission.

The complete original bytes remain unchanged. This pure worker function owns
only an in-memory decoder; media retains its actual file and PROCESSING lease.
The returned evidence is not permission to read a file or send a model request.
"""
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Literal
from PIL import Image, UnidentifiedImageError


@dataclass(frozen=True,slots=True)
class ImageLimits:
    """Values borrowed from the validated image-understanding configuration."""
    image_max_bytes: int
    edge_max: int
    pixel_max: int
    frames: int


@dataclass(frozen=True,slots=True)
class CheckedImage:
    """Whole original bytes and observed decoded properties, with no file path."""
    data: bytes
    sha256: str
    format: Literal['PNG','JPEG']
    width: int
    height: int


@dataclass(frozen=True,slots=True)
class ImageRejected:
    reason: Literal['INPUT_FORMAT_UNSUPPORTED','IMAGE_LIMIT_EXCEEDED','CONTENT_CORRUPT']


def validate_image(raw: bytes, limits: ImageLimits) -> CheckedImage | ImageRejected:
    """Verify complete static data under fixed limits, without trusting MIME names.

    Call on the media worker after acquiring a protected original-byte lease.
    Both container verification and a full bounded pixel decode must finish
    before returning; decoder failure never becomes a sensitive refusal.
    """
    if type(limits) is not ImageLimits or any(type(v) is not int for v in
            (limits.image_max_bytes,limits.edge_max,limits.pixel_max,limits.frames)):
        raise TypeError('Exact immutable image limits are required.')
    if (limits.image_max_bytes,limits.edge_max,limits.pixel_max,limits.frames)!=(1048576,2048,4194304,1):
        raise ValueError('Unsupported image-understanding limits.')
    if type(raw) is not bytes:raise TypeError('Owned complete image bytes are required.')
    if not raw or len(raw)>limits.image_max_bytes:return ImageRejected('IMAGE_LIMIT_EXCEEDED')
    if not (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8')):
        return ImageRejected('INPUT_FORMAT_UNSUPPORTED')
    try:
        with Image.open(BytesIO(raw),formats=['PNG','JPEG']) as image:
            fmt=image.format
            if fmt not in ('PNG','JPEG'):return ImageRejected('INPUT_FORMAT_UNSUPPORTED')
            width,height=image.size
            if (not 1<=width<=limits.edge_max or not 1<=height<=limits.edge_max or width*height>limits.pixel_max
                    or getattr(image,'n_frames',1)!=limits.frames):return ImageRejected('IMAGE_LIMIT_EXCEEDED')
            image.verify()
        with Image.open(BytesIO(raw),formats=['PNG','JPEG']) as image:
            if image.size!=(width,height) or image.format!=fmt:return ImageRejected('CONTENT_CORRUPT')
            image.load()
        return CheckedImage(raw,sha256(raw).hexdigest(),fmt,width,height)
    except (UnidentifiedImageError,OSError,ValueError,SyntaxError):
        return ImageRejected('CONTENT_CORRUPT')
    except Image.DecompressionBombError:
        return ImageRejected('IMAGE_LIMIT_EXCEEDED')
