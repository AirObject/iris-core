"""Fixed runtime-event vocabulary and bounded input rules, without settings defaults."""

from types import MappingProxyType

_LEVEL_NUMBERS = MappingProxyType({
    "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50,
})
_MODULES = (
    "ingress", "runtime", "buffers", "media", "cognition", "memory", "self_model",
    "retrieval", "state", "goals", "dream", "management", "provider",
    "logging_service", "configuration", "bootstrap",
)
_MESSAGES = MappingProxyType({
    "DIAGNOSTIC_READY": "运行诊断服务已就绪。",
    "OPERATION_COMPLETED": "操作已完成。",
    "OPERATION_FAILED": "操作未完成。",
    "DELIVERY_RECOVERED": "诊断输出已恢复。",
})
_MAX_LEVEL_NAME_LENGTH = max(map(len, _LEVEL_NUMBERS))
_MAX_EVENT_CODE_LENGTH = max(map(len, _MESSAGES))
_CONTEXT_FIELDS = (
    "trace_id", "span_id", "request_id", "run_id", "batch_id", "dream_run_id",
    "entry_id", "provider_request_id", "attempt_id",
)
_ATTRIBUTE_FIELDS = ("count", "duration_ms", "outcome", "error_code")
_TOP_FIELDS = ("level", "event_code", "context", "attributes")
_OUTCOMES = ("SUCCESS", "FAILURE", "DEGRADED")
_ERROR_CODES = ("TIMEOUT", "IO_FAILURE", "VALIDATION_FAILED", "INTERNAL_FAILURE")
_MAX_ITEMS = 32
_MAX_KEY_LENGTH = 128
_MAX_ID_LENGTH = 128
_MAX_INTEGER = 2**63 - 1
