"""Actual Logger publication and diagnostic failures are separate from required audit."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
from companion_memory.logging_service import Logger, LoggingOk
from tests.logging_service.service_support import ServiceTestCase
from tests.provider.support import Fixture, completed, success


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_provider_logger_preserves_associations_without_payload(self):
        logging=ServiceTestCase()
        logging.setUp()
        try:
            service=logging.service({"logging.file_enabled":False})
            logger=service.get_logger("provider")
            assert type(logger) is LoggingOk
            with TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(success("private synthetic result"),))
                f.resources=replace(f.resources,logger=logger.value)
                try:
                    await f.initialize()
                    result=completed(await f.work.generate(f.request()))
                    service.flush()
                    lines=[json.loads(line) for line in logging.stderr.getvalue().splitlines()]
                    emitted=[line for line in lines if line["event_code"]=="OPERATION_COMPLETED"]
                    self.assertEqual(len(emitted),1)
                    event=emitted[0]
                    self.assertEqual(event["logger"],"provider")
                    self.assertEqual(event["context"]["request_id"],result.record["object_id"])
                    self.assertEqual(event["context"]["run_id"],"sample_run")
                    self.assertIn("attempt_id",event["context"])
                    self.assertNotIn("private synthetic",repr(lines))
                finally:
                    await f.close()
        finally:
            logging.doCleanups()

    async def test_disabled_faulted_and_raising_logger_do_not_undo_settlement(self):
        logging=ServiceTestCase();logging.setUp()
        try:
            service=logging.service({"logging.file_enabled":False,"logging.console_enabled":False})
            bound=service.get_logger("provider");assert type(bound) is LoggingOk
            with TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(success(),success()))
                f.resources=replace(f.resources,logger=bound.value)
                try:
                    await f.initialize()
                    first=completed(await f.work.generate(f.request()))
                    service.close()
                    with patch.object(Logger,"emit",side_effect=RuntimeError("private sink failure")):
                        second=completed(await f.work.generate(f.request("another")))
                    self.assertEqual([first.record["outcome"],second.record["outcome"]],["SUCCEEDED","SUCCEEDED"])
                    self.assertFalse(f.service.get_health().ledger_faulted)
                finally:
                    await f.close()
        finally:
            logging.doCleanups()
