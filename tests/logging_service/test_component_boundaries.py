"""Confirm pure internal composition and public creation have no external side effects.

The checks guard both successful pure preparation and safe failures. They do not
test lifecycle, queue accounting, emergency delivery, or physical resources.
"""

from contextlib import ExitStack
from importlib import reload
from unittest.mock import patch

import companion_memory.logging_service as logging_service
from companion_memory.logging_service import _encoding
from companion_memory.logging_service._encoding import _EncodedEvent
from companion_memory.logging_service._routing import _route_encoded
from tests.logging_service.support import EventTestCase


class ComponentBoundaryTests(EventTestCase):
    """Use rejecting IO probes around purely internal processing."""

    def test_package_exposes_service_and_creation_is_only_new_state(self):
        for name in ("Service", "Logger", "create_logging_service", "LoggingOk", "LoggingErr"):
            self.assertIn(name, logging_service.__all__)
        self.assertEqual(logging_service.create_logging_service().get_sink_health().lifecycle, "NEW")
        for name in ("append_audit", "query_runtime_logs", "subscribe_runtime_logs", "export_logs"):
            self.assertFalse(hasattr(logging_service, name))

    def test_success_and_failures_never_start_workers_or_touch_output_resources(self):
        settings = self.settings()
        targets = (
            "builtins.open", "io.open", "os.open", "os.stat", "os.mkdir", "os.rename",
            "os.remove", "pathlib.Path.open", "logging.getLogger", "logging.basicConfig",
            "logging.Logger.handle", "threading.Thread.start", "uuid.uuid4", "time.time",
            "sys.stdout.write", "sys.stderr.write",
        )
        with ExitStack() as stack:
            probes = [stack.enter_context(patch(target, side_effect=AssertionError("Unexpected external side effect.")))
                      for target in targets]
            reload(logging_service)
            result = self.prepare(self.event(context={"request_id": "request-7"}), settings)
            if not isinstance(result, _EncodedEvent):
                self.fail("Safe preparation must succeed in memory.")
            decisions = _route_encoded(result, settings)
            self.assertEqual(tuple(item.disposition for item in decisions), ("ELIGIBLE", "ELIGIBLE"))
            self.event_failure(self.prepare(self.event(attributes={"count": True}), settings),
                               "FIELD_VALUE_INVALID", "attributes")
            self.event_failure(self.prepare(self.event(), settings, id_source=lambda: None),
                               "EVENT_BUILD_FAILED", "event", "ADMISSION_REJECTED")
            with patch.object(_encoding, "_encode_jsonl", side_effect=ValueError("secret-demo")):
                self.event_failure(self.prepare(self.event(), settings), "FORMAT_FAILED", "event")
            for probe in probes:
                probe.assert_not_called()
