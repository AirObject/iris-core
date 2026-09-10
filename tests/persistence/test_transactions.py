"""Public all-or-none flow, original-result idempotency and module boundaries."""

from contextlib import closing
import json
import sqlite3
from types import MappingProxyType
from typing import cast

from companion_memory.logging_service import AuditErr, AuditFound
from companion_memory.persistence import (
    Committed, Failed, Found, NotCommitted, NotFound, OperationIdentity,
    OperationPort, Ready, Rejected, StatementDefinition, StatementPort,
)
from tests.persistence.support import DATABASE_ID, SCOPE, Fixture, PersistenceTestCase


class TransactionTests(PersistenceTestCase):
    async def test_transfer_publishes_both_modules_two_audits_and_original_receipt(self):
        fixture = self.fixture
        result = await fixture.operation.execute("move_7", fixture.command())
        assert type(result) is Committed
        result = cast(Committed, result)
        self.assertEqual(fixture.raw_counts(), (7, 3, 1, 2))
        assert type(result.receipt.result) is MappingProxyType
        self.assertEqual(result.receipt.result["source_units"], 7)
        self.assertEqual(result.source, "NEW")
        self.assertIs(type(result.receipt.result), MappingProxyType)
        with self.assertRaises(TypeError):
            cast(dict[str, object], result.receipt.result)["source_units"] = 99
        point = await fixture.source_read.read_object({"object_id": "source-counter"})
        assert type(point) is Found
        assert type(point.value) is tuple and type(point.value[0]) is MappingProxyType
        self.assertEqual(point.value[0]["units"], 7)
        assert fixture.reader is not None
        audit = await fixture.reader.read_audit(result.receipt.identity)
        assert type(audit) is AuditFound
        self.assertEqual(tuple(item.event_slot for item in audit.records), ("source_changed", "target_changed"))
        self.assertIs(type(audit.records[0].target_refs), tuple)
        self.assertIs(type(audit.records[0].change), MappingProxyType)
        with self.assertRaises(TypeError):
            cast(tuple[dict[str, object], ...], audit.records[0].target_refs)[0]["revision"] = 99
        read = await fixture.operation.read_receipt("move_7")
        assert type(read) is Found
        self.assertEqual(read.value, result.receipt)

    async def test_identical_retry_returns_original_after_later_business_changes(self):
        fixture = self.fixture
        original = fixture.command()
        first = await fixture.operation.execute("move_7", original)
        assert type(first) is Committed
        second = await fixture.operation.execute("move_8", fixture.command(2, 1, 1, 7, 3))
        assert type(second) is Committed
        repeat = await fixture.operation.execute("move_7", original)
        assert type(repeat) is Committed
        self.assertEqual(repeat.receipt, first.receipt)
        self.assertEqual(repeat.source, "EXISTING")
        self.assertEqual((fixture.handler_calls, fixture.raw_counts()), (2, (5, 5, 2, 4)))

    async def test_changed_quantity_revision_actor_or_policy_conflicts_without_replay(self):
        fixture = self.fixture
        first = await fixture.operation.execute("move_7", fixture.command())
        assert type(first) is Committed
        changed = [fixture.command(2), fixture.command(3, 1, 1)]
        actor = fixture.command()
        cast(dict[str, dict[str, object]], actor.audit_events)["source_changed"]["actor_ref"] = "other-actor"
        changed.append(actor)
        for command in changed:
            result = await fixture.operation.execute("move_7", command)
            assert type(result) is Rejected
            self.assertEqual(result.error.reason, "CONTENT_MISMATCH")
        self.assertEqual((fixture.handler_calls, fixture.raw_counts()), (1, (7, 3, 1, 2)))

    async def test_close_reopen_keeps_identity_schema_and_original_results(self):
        fixture = self.fixture
        command = fixture.command()
        original = await fixture.operation.execute("move_7", command)
        assert type(original) is Committed
        await fixture.service.close()
        reopened = Fixture(self.directory)
        try:
            self.assertIs(type(await reopened.initialize("OPEN_EXISTING")), Ready)
            repeat = await reopened.operation.execute("move_7", command)
            assert type(repeat) is Committed
            self.assertEqual(repeat.receipt, original.receipt)
            self.assertEqual((repeat.source, reopened.handler_calls), ("EXISTING", 0))
        finally:
            await reopened.service.close()

    async def test_business_failure_after_each_participant_rolls_back_every_effect(self):
        fixture = self.fixture
        for point in ("after_source", "after_target", "after_audits"):
            def fail(at, uow):
                if at == point:
                    raise RuntimeError("secret-synthetic-detail")
            fixture.local_hook = fail
            result = await fixture.operation.execute(point, fixture.command())
            assert type(result) is NotCommitted and result.error is not None
            self.assertEqual(result.error.reason, "PARTICIPANT_REJECTED")
            self.assertNotIn("secret-synthetic-detail", repr(result))
            self.assertEqual(fixture.raw_counts(), (10, 0, 0, 0))

    async def test_caught_failed_participant_still_poisons_the_uow(self):
        fixture = self.fixture
        seen = []
        def fail_and_catch(at, uow):
            if at == "after_source":
                seen.append(fixture.target_write.participate(uow, {"object_id": "target-counter", "delta": -100, "expected_revision": 0}))
        fixture.local_hook = fail_and_catch
        result = await fixture.operation.execute("move_7", fixture.command())
        self.assertIs(type(seen[0]), Failed)
        assert type(result) is NotCommitted and result.error is not None
        self.assertEqual(result.error.reason, "CONSTRAINT_FAILED")
        self.assertEqual(fixture.raw_counts(), (10, 0, 0, 0))

    async def test_missing_audit_is_a_mandatory_failure_without_diagnostics(self):
        fixture = self.fixture
        fixture.skip_audit = "target_changed"
        result = await fixture.operation.execute("move_7", fixture.command())
        assert type(result) is NotCommitted and result.error is not None
        self.assertEqual((result.error.code, result.error.reason, result.error.field), ("TRANSACTION_FAILED", "AUDIT_REQUIRED", "audit"))
        self.assertEqual(fixture.raw_counts(), (10, 0, 0, 0))

    async def test_duplicate_audit_failure_cannot_be_caught_to_commit(self):
        fixture = self.fixture
        def duplicate(at, uow):
            if at == "after_audits":
                result = fixture.audits["source_changed"].append_audit(uow, fixture.events["source_changed"])
                assert type(result) is AuditErr
                self.assertEqual(result.error.reason, "AUDIT_EVENT_CONFLICT")
        fixture.local_hook = duplicate
        result = await fixture.operation.execute("move_7", fixture.command())
        assert type(result) is NotCommitted and result.error is not None
        self.assertEqual(result.error.reason, "AUDIT_FAILED")
        self.assertEqual(fixture.raw_counts(), (10, 0, 0, 0))

    async def test_forged_native_statement_and_operation_ports_have_no_authority(self):
        fixture = self.fixture
        fake_statement = StatementDefinition("UPDATE source_counts SET units=999 RETURNING units, revision", fixture.source.statements[0].parameters, fixture.source.statements[0].row_schema, True)
        fake = StatementPort(fixture.service, fixture.source, fake_statement, SCOPE)
        def attempt(at, uow):
            if at == "after_source":
                failure = fake.participate(uow, {"object_id": "source-counter", "delta": 1, "expected_revision": 1})
                assert type(failure) is Failed
                self.assertEqual(failure.error.reason, "CAPABILITY_MISMATCH")
        fixture.local_hook = attempt
        result = await fixture.operation.execute("move_7", fixture.command())
        assert type(result) is NotCommitted and result.error is not None
        self.assertEqual(result.error.reason, "CAPABILITY_MISMATCH")
        forged = OperationPort(fixture.service, fixture.command_definition, SCOPE)
        denied = await forged.execute("move_7", fixture.command())
        assert type(denied) is Rejected
        self.assertEqual(denied.error.reason, "CAPABILITY_MISMATCH")
        with self.assertRaises(AttributeError):
            fixture.operation._scope = "other"

    async def test_cross_scope_statement_expired_uow_and_developer_reads_are_rejected(self):
        fixture = self.fixture
        other = fixture.service.bind_statement(fixture.source, fixture.source.statements[0], "other_scope")
        saved = []
        def attempt(at, uow):
            if at == "after_source":
                saved.append(uow)
                failure = other.participate(uow, {})
                assert type(failure) is Failed
                self.assertEqual(failure.error.reason, "CAPABILITY_MISMATCH")
        fixture.local_hook = attempt
        result = await fixture.operation.execute("move_7", fixture.command())
        assert type(result) is NotCommitted and result.error is not None
        expired = fixture.source_write.participate(saved[0], {})
        assert type(expired) is Failed
        self.assertEqual(expired.error.reason, "CAPABILITY_MISMATCH")
        self.assertIs(type(await fixture.audits["source_changed"].read_audit(object())), AuditErr)
        assert fixture.reader is not None
        bad = OperationIdentity(DATABASE_ID, "transfer", "transfer_units", "other_scope", "move_7")
        denied = await fixture.reader.read_audit(bad)
        assert type(denied) is AuditErr
        self.assertEqual(denied.error.reason, "AUDIT_ACCESS_DENIED")

    async def test_no_receipt_is_only_observation_and_fresh_confirmation_is_separate(self):
        fixture = self.fixture
        command = fixture.command()
        observation = await fixture.operation.read_receipt("move_7")
        assert type(observation) is NotFound
        handle = fixture.operation.recovery_handle("move_7", command)
        confirmed = await fixture.operation.resolve_operation(handle)
        assert type(confirmed) is NotCommitted
        self.assertIsNone(confirmed.error)
        self.assertEqual(fixture.handler_calls, 0)

    async def test_orphan_audit_is_integrity_failure_and_faults_storage(self):
        fixture = self.fixture
        result = await fixture.operation.execute("move_7", fixture.command())
        assert type(result) is Committed
        with closing(sqlite3.connect(fixture.path)) as connection, connection:
            connection.execute("DELETE FROM operation_receipts")
        assert fixture.reader is not None
        read = await fixture.reader.read_audit(result.receipt.identity)
        assert type(read) is AuditErr
        self.assertEqual(read.error.reason, "AUDIT_INCONSISTENT")
        self.assertEqual(fixture.service.get_health().lifecycle, "FAULTED")

    async def test_corrupt_boolean_audit_version_is_not_treated_as_integer_one(self):
        fixture = self.fixture
        result = await fixture.operation.execute("move_7", fixture.command())
        assert type(result) is Committed
        with closing(sqlite3.connect(fixture.path)) as connection, connection:
            slot, encoded = connection.execute("SELECT event_slot, record FROM audit_records LIMIT 1").fetchone()
            record = json.loads(encoded)
            record["schema_version"] = True
            connection.execute("UPDATE audit_records SET record=? WHERE event_slot=?", (json.dumps(record).encode(), slot))
        read = await fixture.operation.read_receipt("move_7")
        assert type(read) is Failed
        self.assertEqual(read.error.reason, "DATA_INCONSISTENT")

    async def test_provider_style_side_effect_runs_only_after_confirmed_registration(self):
        fixture = self.fixture
        markers = []
        fixture.skip_audit = "source_changed"
        result = await fixture.operation.execute("register", fixture.command())
        if type(result) is Committed:
            markers.append("external-action")
        self.assertEqual(markers, [])
        fixture.skip_audit = None
        original = fixture.command()
        result = await fixture.operation.execute("register", original)
        if type(result) is Committed:
            markers.append("external-action")
        self.assertEqual(markers, ["external-action"])
        repeat = await fixture.operation.execute("register", original)
        assert type(repeat) is Committed
        self.assertEqual(repeat.source, "EXISTING")
