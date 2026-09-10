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

__all__ = [
    "BinaryOutput", "LoggingResources", "CloseReport", "DeliveryDecision", "EmergencyDisposition",
    "EmitReceipt", "FlushReport", "HealthSnapshot", "Lifecycle", "LoggingErr", "LoggingError",
    "LoggingOk", "LoggingResult", "Operation", "SinkHealth", "SinkReport", "Thresholds",
    "Logger", "Service", "create_logging_service",
]
