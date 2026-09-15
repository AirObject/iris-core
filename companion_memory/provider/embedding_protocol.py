"""Closed dense-text embedding wire protocol and exact token liability.

This codec owns no credentials or transport. Parsing preserves usage independently
of result validity. Only an embedding Provider adapter may use the returned
material; a parsed response never grants sending or settlement authority.
"""
from dataclasses import dataclass
import json
import math
from types import MappingProxyType
from typing import cast
from .values import Data, Record, InvalidData, DataLimit, as_record, freeze
from .token_costs import quantity, rounded_cost

PROTOCOL = 'ARK_CODING_DENSE_TEXT_V1'
DIMENSION = 1024
WIRE_LIMIT = 65536
NORMALIZED_LIMIT = 40960
NUMBER_TOKEN_LIMIT = 32


def _number(token: str) -> float:
    if len(token.encode('ascii')) > NUMBER_TOKEN_LIMIT:
        raise DataLimit()
    value = float(token)
    if not math.isfinite(value):
        raise InvalidData()
    return value


def _integer(token: str) -> int:
    if len(token) > NUMBER_TOKEN_LIMIT:
        raise DataLimit()
    return int(token)


def _constant(token: str) -> object:
    raise InvalidData()


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise InvalidData()
        result[key] = value
    return result


def _parse(raw: bytes) -> Record:
    if type(raw) is not bytes or len(raw) > WIRE_LIMIT:
        raise DataLimit()
    # Bound nesting before allocating any JSON containers.
    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped: escaped = False
            elif byte == 92: escaped = True
            elif byte == 34: quoted = False
        elif byte == 34: quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > 8: raise DataLimit()
        elif byte in (93, 125): depth -= 1
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_float=_number, parse_int=_integer, parse_constant=_constant)
        def own(item: object) -> Data:
            if type(item) is dict:
                return MappingProxyType({k:own(v) for k,v in item.items()})
            if type(item) is list: return tuple(own(v) for v in item)
            if item is None or type(item) in (str,int,float,bool): return cast(Data,item)
            raise InvalidData()
        return as_record(own(value))
    except (UnicodeError, ValueError, OverflowError, RecursionError):
        raise InvalidData() from None


def request_bytes(payload: Record, model_id: str = 'doubao-embedding-vision') -> bytes:
    """Validate the complete native request and encode one unchanged text item.

    DOCUMENT accepts at most 8192 UTF-8 bytes; QUERY accepts at most 512. There
    are no instruction prefixes, retries or endpoint fallback parameters.
    """
    if type(payload) is not MappingProxyType or set(payload) != {'texts', 'purpose', 'dimensions'}:
        raise InvalidData()
    if payload['purpose'] not in ('DOCUMENT', 'QUERY') or type(payload['dimensions']) is not int or payload['dimensions'] != DIMENSION:
        raise InvalidData()
    texts = payload['texts']
    if type(texts) is not tuple or len(texts) != 1 or type(texts[0]) is not str:
        raise InvalidData()
    if model_id != 'doubao-embedding-vision': raise InvalidData()
    try:
        size = len(texts[0].encode('utf-8'))
        if not 1 <= size <= (8192 if payload['purpose'] == 'DOCUMENT' else 512): raise DataLimit()
        raw = json.dumps({'model': model_id, 'input': list(texts), 'encoding_format': 'float',
                          'dimensions': DIMENSION}, ensure_ascii=False, allow_nan=False,
                         separators=(',', ':')).encode('utf-8')
    except (UnicodeError, ValueError):
        raise InvalidData() from None
    if len(raw) > WIRE_LIMIT: raise DataLimit()
    return raw


@dataclass(frozen=True, slots=True)
class EmbeddingUsage:
    """Reported input tokens with no inferred cached-token count or bill."""
    input_tokens: int
    total_tokens: int
    input_items: int = 1
    cache_read_tokens: None = None

    def estimate(self, input_bound: int, atoms_per_million: int) -> int:
        """Return a conservative local estimate; exceeding liability is an error."""
        if self.input_tokens > quantity(input_bound): raise InvalidData()
        return rounded_cost(self.input_tokens, atoms_per_million)


@dataclass(frozen=True, slots=True)
class EmbeddingResponse:
    """Independent result and usage validation, retaining a safe first error."""
    result: Record | None
    usage: EmbeddingUsage | None
    error: str


def parse_response(raw: bytes, *, space_id: str, expected_models: tuple[str, ...], usage_only:bool=False) -> EmbeddingResponse:
    """Reject duplicate keys, malformed identity and nonfinite or zero vectors.

    Complete known usage survives an otherwise invalid result. Invalid JSON has
    no reliable usage. This function does not classify remote completion or
    release held budget; those decisions belong to the original Provider owner.
    """
    from .values import is_identifier
    from companion_memory.configuration.semantic_schema import REPORTED_MODELS
    if (type(usage_only) is not bool or not is_identifier(space_id) or type(expected_models) is not tuple
            or not 1<=len(expected_models)<=(2 if usage_only else 1) or not all(is_identifier(v) for v in expected_models)
            or usage_only and (len(set(expected_models))!=len(expected_models) or not set(expected_models)<=set(REPORTED_MODELS))):
        raise InvalidData()
    try:
        value = _parse(raw)
    except InvalidData:
        return EmbeddingResponse(None, None, 'PROTOCOL')
    usage: EmbeddingUsage | None = None
    reported = value.get('usage')
    if type(reported) is MappingProxyType and set(reported) == {'prompt_tokens', 'total_tokens'}:
        incoming, total = reported['prompt_tokens'], reported['total_tokens']
        if type(incoming) is int and type(total) is int and 0 <= incoming == total <= 2**63-1:
            usage = EmbeddingUsage(incoming, total)
    try:
        if set(value) != {'id', 'created', 'model', 'object', 'data', 'usage'} or value['object'] != 'list':
            raise InvalidData()
        if not is_identifier(value['id']) or value['model'] not in expected_models or type(value['created']) is not int or not 0 <= value['created'] <= 2**63-1:
            return EmbeddingResponse(None, usage, 'IDENTITY')
        data = value['data']
        if type(data) is not tuple or len(data) != 1: raise InvalidData()
        item = as_record(data[0])
        if set(item) != {'object', 'index', 'embedding'} or item['object'] != 'embedding' or type(item['index']) is not int or item['index'] != 0:
            raise InvalidData()
        vector = item['embedding']
        if type(vector) is not tuple or len(vector) != DIMENSION or any(type(n) not in (int, float) for n in vector): raise InvalidData()
        numbers = tuple(float(cast(int | float, n)) for n in vector)
        if not all(math.isfinite(n) for n in numbers) or not any(numbers): raise InvalidData()
        result = as_record(freeze({'vectors': (numbers,), 'dimensions': DIMENSION,
            'space_id': space_id, 'model_id': 'doubao-embedding-vision', 'input_items': 1}, NORMALIZED_LIMIT))
        return EmbeddingResponse(result, usage, 'NONE' if usage is not None else 'USAGE')
    except (InvalidData, ValueError, OverflowError):
        return EmbeddingResponse(None, usage, 'PROTOCOL')
