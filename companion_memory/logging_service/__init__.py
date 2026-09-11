"""Public bounded runtime diagnostics for borrowed console streams and JSONL files.

Import and service creation have no I/O or global handler side effects. Trusted
assembly supplies a checked native configuration snapshot and explicit resources;
receipts and flush/close reports do not promise media persistence.
"""

from .resources import BinaryOutput, LoggingResources
from .results import (
    CloseReport, DeliveryDecision, EmergencyDisposition, EmitReceipt, FlushReport,
    HealthSnapshot, Lifecycle, LoggingErr, LoggingError, LoggingOk, LoggingResult,
    Operation, SinkHealth, SinkReport, Thresholds,
)
from .service import Logger, Service, create_logging_service
from .audit import AuditAccess, AuditBound, bind_audit
from .audit_records import (
    AuditComplete, AuditErr, AuditError, AuditFound, AuditNotFound, AuditRecord,
    AuditRequirement, AuditStaged,
)

__all__ = [
    "BinaryOutput", "LoggingResources", "CloseReport", "DeliveryDecision", "EmergencyDisposition",
    "EmitReceipt", "FlushReport", "HealthSnapshot", "Lifecycle", "LoggingErr", "LoggingError",
    "LoggingOk", "LoggingResult", "Operation", "SinkHealth", "SinkReport", "Thresholds",
    "Logger", "Service", "create_logging_service",
    "AuditAccess", "AuditBound", "AuditComplete", "AuditErr", "AuditError", "AuditFound",
    "AuditNotFound", "AuditRecord", "AuditRequirement", "AuditStaged", "bind_audit",
]

from .runtime_window import RuntimeLogWindow, RuntimeLogReader, ObservationGrant, LogPage, LogGap, LogReadFailed
__all__ += ['RuntimeLogWindow','RuntimeLogReader','ObservationGrant','LogPage','LogGap','LogReadFailed']
