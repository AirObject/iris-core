"""Native capabilities cannot cross two physical stores sharing identity strings."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService
from companion_memory.persistence import Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider import SimulationAdapter, Scenario, ResultGrant
from companion_memory.provider.terminal_evidence import TerminalVerified, VerifiedTerminal
from tests.memory.support import Fixture
from tests.media.test_guard_dispatch_competition import window
from tests.provider.support import success


class NativeOwnerBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_database_name_does_not_authorize_foreign_byte_owner_or_terminal(self):
        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            proposal = SyntheticCandidateInput('native:1', ('synthetic fact',), 50)
            first = Fixture(Path(first_directory), MediaService(), proposal)
            first.adapter = SimulationAdapter((Scenario('SENSITIVE_REFUSAL', None,
                {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}), success()))
            await first.initialize()
            second = await Fixture(Path(second_directory), MediaService(), proposal).initialize()
            assert first.media is not None and second.media is not None
            proofs = []
            try:
                self.assertEqual(first.expected_id, second.expected_id)
                self.assertIsNot(first.storage, second.storage)
                with self.assertRaises(ValueError):
                    first.provider.bind_stored_media_authority(second.media, 'instance', 'media')
                with self.assertRaises(ValueError): second.media.work.retain_terminal(object.__new__(VerifiedTerminal))
                retain = first.media.work.retain_terminal
                def inspect(evidence):
                    assert second.media is not None
                    proofs.append(evidence)
                    with self.assertRaises(ValueError): second.media.work.retain_terminal(evidence)
                    return retain(evidence)
                first.media.work.retain_terminal = inspect
                entry, _ = await window(first, 'entry')
                result = await entry.run_learning('original'); assert type(result) is Committed, result
                self.assertEqual(len(proofs), 1)
                self.assertFalse(second.media.work.evidence)
                runtime = first.runtime; assert runtime is not None
                from companion_memory.runtime.content_assembly import stable
                prep = stable('preparation', first.expected_id, 'entry', 'original')
                bid = stable('batch', prep)
                work = (await first.assembly.rows.read('work_get', {'batch_id': bid}))[0]
                request_id = cast(str, work['provider_request_id'])
                proof = await first.provider.bind_result_owner(ResultGrant('cognition', (request_id,))).verify_terminal(request_id)
                assert type(proof) is TerminalVerified, proof
                with self.assertRaises(OwnerFailure): second.assembly.retain_learning_terminal(proof.value)
                self.assertFalse(second.assembly._verified_terminals)
                self.assertEqual(len(first.adapter.calls), 2)
                self.assertEqual(len(second.adapter.calls), 0)
            finally:
                proofs.clear()
                await first.close(); await second.close()
