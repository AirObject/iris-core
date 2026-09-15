"""Exact immutable dense-vector file codec with bounded streaming validation.

Files contain original binary64 bytes, never renormalized stored coordinates.
Readers verify every binding and the complete record digest before scanning.
This module neither publishes database pointers nor grants memory permissions.
"""
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from hashlib import sha256
import heapq
import json
import math
import struct
from typing import BinaryIO
from companion_memory.persistence.schema import valid_identifier

HEADER_BYTES = 4096
RECORD_BYTES = 8384
DIMENSION = 1024
VECTOR_BYTES = 8192
MEMBER_LIMIT = 4096
PAGE_MEMBERS = 8
FILE_LIMIT = HEADER_BYTES + MEMBER_LIMIT * RECORD_BYTES


class InvalidVectorFile(ValueError):
    """An incomplete or inconsistent generation cannot be used for retrieval."""


def _domain(identity: str, prefix: str) -> bytes:
    if type(identity) is not str or not identity.startswith(prefix + ':'):
        raise InvalidVectorFile('Invalid vector identity.')
    digest = identity[len(prefix)+1:]
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise InvalidVectorFile('Invalid vector identity.')
    return bytes.fromhex(digest)


def vector_bytes(vector: tuple[float, ...]) -> bytes:
    """Keep every finite binary64 coordinate, including the sign of zero."""
    if type(vector) is not tuple or len(vector) != DIMENSION or any(type(n) is not float or not math.isfinite(n) for n in vector) or not any(vector):
        raise InvalidVectorFile('A complete finite nonzero vector is required.')
    return struct.pack('<1024d', *vector)


def validate_vector(raw: bytes) -> None:
    if type(raw) is not bytes or len(raw) != VECTOR_BYTES:
        raise InvalidVectorFile('Invalid vector byte count.')
    nonzero = False
    for (number,) in struct.iter_unpack('<d', raw):
        if not math.isfinite(number): raise InvalidVectorFile('Nonfinite vector coordinate.')
        nonzero |= number != 0
    if not nonzero: raise InvalidVectorFile('Zero vector norm.')


@dataclass(frozen=True, slots=True)
class VectorMember:
    """One frozen object revision and its immutable paid artifact binding."""
    object_id: str
    object_revision: int
    ack_revision: int
    applied_seq: int
    artifact_id: str
    vector: bytes

    @property
    def vector_digest(self) -> str:
        return sha256(self.vector).hexdigest()

    def binding_bytes(self) -> bytes:
        return json.dumps([self.object_id, self.object_revision, self.ack_revision,
            self.applied_seq, self.artifact_id, self.vector_digest], ensure_ascii=False,
            separators=(',', ':')).encode('utf-8')

    def encode(self) -> bytes:
        if not valid_identifier(self.object_id) or any(type(n) is not int or not 1 <= n <= 2**63-1 for n in (self.object_revision, self.ack_revision, self.applied_seq)):
            raise InvalidVectorFile('Invalid object binding.')
        validate_vector(self.vector)
        identity = self.object_id.encode('ascii')
        return (struct.pack('<H128s2xQQ', len(identity), identity, self.object_revision, self.applied_seq)
                + _domain(self.artifact_id, 'embedding-artifact') + struct.pack('<Q4x', self.ack_revision) + self.vector)

    @classmethod
    def decode(cls, raw: bytes) -> 'VectorMember':
        if len(raw) != RECORD_BYTES: raise InvalidVectorFile('Incomplete member record.')
        length = struct.unpack_from('<H', raw)[0]
        if not 1 <= length <= 128 or any(raw[2+length:132]) or any(raw[188:192]):
            raise InvalidVectorFile('Noncanonical member padding.')
        try: object_id = raw[2:2+length].decode('ascii')
        except UnicodeError: raise InvalidVectorFile('Invalid object identifier.') from None
        revision, seq = struct.unpack_from('<QQ', raw, 132)
        ack = struct.unpack_from('<Q', raw, 180)[0]
        member = cls(object_id, revision, ack, seq, 'embedding-artifact:' + raw[148:180].hex(), raw[192:])
        if member.encode() != raw: raise InvalidVectorFile('Noncanonical member.')
        return member


@dataclass(frozen=True, slots=True)
class VectorHeader:
    """Complete header identity, counts and digests; reserved bytes stay zero."""
    space_id: str
    generation_id: str
    captured_seq: int
    member_count: int
    member_digest: bytes
    records_digest: bytes

    def encode(self) -> bytes:
        if (type(self.member_count) is not int or not 0 <= self.member_count <= MEMBER_LIMIT
                or type(self.captured_seq) is not int or not 0 <= self.captured_seq <= 2**63-1
                or type(self.member_digest) is not bytes or len(self.member_digest) != 32
                or type(self.records_digest) is not bytes or len(self.records_digest) != 32):
            raise InvalidVectorFile('Invalid vector header.')
        raw = bytearray(HEADER_BYTES)
        struct.pack_into('<8sIIIIIIQQ', raw, 0, b'IRISVEC1', 1, HEADER_BYTES, DIMENSION, RECORD_BYTES,
            self.member_count, (self.member_count+7)//8, self.captured_seq, HEADER_BYTES)
        raw[48:80] = _domain(self.space_id, 'embedding-space')
        raw[80:112] = _domain(self.generation_id, 'semantic-generation')
        raw[112:144], raw[144:176] = self.member_digest, self.records_digest
        struct.pack_into('<QII', raw, 176, HEADER_BYTES + self.member_count*RECORD_BYTES, 0x01020304, 1)
        return bytes(raw)

    @classmethod
    def decode(cls, raw: bytes, space_id: str, generation_id: str) -> 'VectorHeader':
        if len(raw) != HEADER_BYTES: raise InvalidVectorFile('Incomplete vector header.')
        header = cls(space_id, generation_id, struct.unpack_from('<Q', raw, 32)[0],
            struct.unpack_from('<I', raw, 24)[0], raw[112:144], raw[144:176])
        if header.encode() != raw: raise InvalidVectorFile('Vector header binding or format differs.')
        return header


def write_generation(file: BinaryIO, space_id: str, generation_id: str, captured_seq: int,
                     members: Iterable[VectorMember], checkpoint: Callable[[], None]) -> VectorHeader:
    """Stream one generation, retaining at most one full member in memory.

    The caller owns the unpublished file and must fsync, rename and prove
    publication separately. On error this partial file is never query-ready.
    """
    file.seek(0); file.truncate(); file.write(bytes(HEADER_BYTES))
    records, bindings = sha256(), sha256(b'[')
    count = 0; previous = ''
    for member in members:
        if count % 8 == 0: checkpoint()
        if count >= MEMBER_LIMIT or member.object_id <= previous or member.applied_seq > captured_seq:
            raise InvalidVectorFile('Members are unordered or exceed the captured generation.')
        raw = member.encode()
        if file.write(raw) != len(raw): raise OSError('Incomplete vector file write.')
        records.update(raw)
        if count: bindings.update(b',')
        bindings.update(member.binding_bytes())
        count += 1; previous = member.object_id
    bindings.update(b']')
    header = VectorHeader(space_id, generation_id, captured_seq, count, bindings.digest(), records.digest())
    file.seek(0)
    if file.write(header.encode()) != HEADER_BYTES: raise OSError('Incomplete vector header write.')
    file.flush(); checkpoint()
    return header


def read_members(file: BinaryIO, header: VectorHeader, checkpoint: Callable[[], None]) -> Iterator[VectorMember]:
    """Stream and verify a complete generation; callers must exhaust validation."""
    file.seek(0)
    if file.read(HEADER_BYTES) != header.encode(): raise InvalidVectorFile('Header mismatch.')
    records, bindings = sha256(), sha256(b'[')
    previous = ''
    for ordinal in range(header.member_count):
        if ordinal % 16 == 0: checkpoint()
        raw = file.read(RECORD_BYTES); member = VectorMember.decode(raw)
        if member.object_id <= previous or member.applied_seq > header.captured_seq:
            raise InvalidVectorFile('Invalid member ordering or sequence.')
        records.update(raw)
        if ordinal: bindings.update(b',')
        bindings.update(member.binding_bytes()); previous = member.object_id
        yield member
    bindings.update(b']')
    if file.read(1) or records.digest() != header.records_digest or bindings.digest() != header.member_digest:
        raise InvalidVectorFile('Incomplete generation digest or trailing bytes.')
    checkpoint()


def cosine(left: bytes, right: bytes) -> float:
    """Scale before summation to avoid overflow and preserve threshold accuracy."""
    validate_vector(left); validate_vector(right)
    a = max(abs(n) for (n,) in struct.iter_unpack('<d', left))
    b = max(abs(n) for (n,) in struct.iter_unpack('<d', right))
    norm_a = math.sqrt(math.fsum((n/a)**2 for (n,) in struct.iter_unpack('<d', left)))
    norm_b = math.sqrt(math.fsum((n/b)**2 for (n,) in struct.iter_unpack('<d', right)))
    result = math.fsum((x/a)*(y/b) for (x,), (y,) in zip(struct.iter_unpack('<d', left), struct.iter_unpack('<d', right))) / (norm_a*norm_b)
    return max(-1.0, min(1.0, result))


def scan(file: BinaryIO, header: VectorHeader, query: bytes, checkpoint: Callable[[], None], *,
         limit: int, minimum_millionths: int) -> tuple[tuple[str, float], ...]:
    """Admit by absolute cosine before ranking, returning only a bounded list.

    No candidate escapes if the final generation digest is invalid. The caller
    must still recheck current permissions and object revisions at delivery.
    """
    if type(limit) is not int or not 1 <= limit <= 64 or minimum_millionths != 700000:
        raise InvalidVectorFile('Unsupported semantic admission policy.')
    validate_vector(query)
    candidates: list[tuple[float, str]] = []
    for member in read_members(file, header, checkpoint):
        score = cosine(member.vector, query)
        if score >= minimum_millionths / 1_000_000:
            candidates.append((-score, member.object_id))
            candidates = heapq.nsmallest(limit, candidates)
    return tuple((identity, -negative) for negative, identity in sorted(candidates))
