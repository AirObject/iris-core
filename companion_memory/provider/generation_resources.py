"""Native fixed Chat adapter and explicit generation resource bindings.

Only Provider invokes the adapter. Configuration binds every schema and endpoint;
credentials are resolved inside the actual transport, never during local result
recovery. Completion callbacks observe the ended worker, not cancellation intent.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from .chat_protocol import ChatBinding, encode_request, decode_response, observe_usage, UsageObservation
from .chat_transport import ChatTransport
from .credentials import CredentialResolver
from .resources import AdapterResponse, CancellationToken, GateBinding
from .values import Record, as_record, freeze, InvalidData


def observation_value(value: UsageObservation, *, not_sent: bool = False) -> Record:
    return MappingProxyType({'fields': value.fields, 'raw_usage': value.raw_usage,
        'valid': value.valid, 'billing_covered': value.billing_covered, 'not_sent': not_sent})


def usage_observation(value: object) -> UsageObservation:
    """Reconstitute only a complete adapter-owned safe usage observation."""
    value = as_record(freeze(value, 4096, owned=True))
    if (set(value) != {'fields', 'raw_usage', 'valid', 'billing_covered', 'not_sent'}
            or any(type(value[k]) is not bool for k in ('valid', 'billing_covered', 'not_sent'))):
        raise InvalidData()
    return UsageObservation(as_record(value['fields']), as_record(value['raw_usage']), value['valid'] is True, value['billing_covered'] is True)


class ChatGenerationAdapter:
    """One configured role-specific wire request, without hidden retries."""
    def __init__(self, transport: ChatTransport, learning: ChatBinding, persona: ChatBinding):
        if type(transport) is not ChatTransport or transport._format!='GENERATION' or any(type(v) is not ChatBinding for v in (learning, persona)):
            raise InvalidData()
        if learning.schema_name != 'text_learning' or persona.schema_name != 'initial_persona':
            raise InvalidData()
        self.transport = transport
        self.bindings = MappingProxyType({'LEARNING': learning, 'PERSONA': persona})

    def invoke(self, request: Record, role: str, cancellation: CancellationToken, deadline: float) -> AdapterResponse:
        """Return protocol facts; the durable owner alone decides a local terminal."""
        binding = self.bindings.get(role)
        if binding is None:
            raise InvalidData()
        body = encode_request(request['payload'], binding)
        result = self.transport.exchange(body, deadline, cancellation)
        unknown = observation_value(observe_usage(None), not_sent=result.state == 'NOT_SENT')
        if result.body is not None and result.status != 200:
            # Bounded usage observations survive an HTTP error independently of
            # output validity. They do not prove final billing or remote success.
            from .chat_json import decode_wire
            try:
                response = as_record(decode_wire(result.body, 262144))
                observer=observe_usage
                if binding.requested_model=='deepseek-flash':
                    from .deepseek_protocol import observe_usage as observer
                if binding.requested_model=='MiniMax-M3':
                    from .minimax_protocol import observe_usage as observer
                observation = observer(response.get('usage'))
                unknown = observation_value(UsageObservation(observation.fields, observation.raw_usage, observation.valid, False))
            except InvalidData:
                pass
        if result.state == 'NOT_SENT':
            return AdapterResponse('LOCAL_NOT_SENT', None, unknown)
        if result.state == 'REMOTE_RESULT_UNKNOWN' or result.status is None or result.status == 429 or result.status >= 500:
            return AdapterResponse('REMOTE_RESULT_UNKNOWN', None, unknown)
        if result.status in (401, 403):
            return AdapterResponse('AUTHENTICATION_FAILED', None, unknown)
        if result.status != 200 or result.body is None:
            return AdapterResponse('INVALID_RESPONSE', None, unknown)
        response = decode_response(result.body, binding)
        return AdapterResponse(response.outcome, response.result, observation_value(response.usage))


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Fixed actual worker and registered-work capacity shared by both roles."""
    max_in_flight: int
    registered_work_limit: int

    def __post_init__(self):
        if any(type(value) is not int or value != 1 for value in (self.max_in_flight, self.registered_work_limit)):
            raise InvalidData()


@dataclass(frozen=True, slots=True)
class GenerationBound:
    """Resource preflight succeeded; this does not publish Provider readiness."""
    database_id: str
    config_snapshot_id: str


@dataclass(frozen=True, slots=True)
class RealGenerationResources:
    """Borrowed native resources; references are resolved only for actual sends."""
    gate: GateBinding
    adapter: ChatGenerationAdapter
    credential_resolver: CredentialResolver
    monotonic: Callable[[], float]
    utc_now: Callable[[], datetime]
    new_id: Callable[[], str]
    completed: Callable[[str], None]
    logger: object = None
