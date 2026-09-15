"""Persistent resource stop and completion reserve without reduced limits.

These are admission-component tests, not a 1000-transaction or large-volume
qualification. Free-space failure is injected into the observation syscall; a
real journal and its original opening are used for cumulative counter recovery.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from companion_memory.persistence.semantic_admission import SemanticAdmission
from companion_memory.persistence.owned_statements import OwnerFailure
from tests.semantic.configuration_support import candidate


class AdmissionTests(unittest.TestCase):
    def test_native_limits_survive_original_reopen_and_only_completion_uses_reserve(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();config,_=candidate(root,offline=False)
            admission=SemanticAdmission(config,root,'CREATE_NEW')
            try:
                for n in range(999):admission.admit_external('fixture_operation','fixture','operation:'+str(n),'a'*64)
                self.assertEqual(admission.observe()['normal'],1000)
                with self.assertRaises(OwnerFailure):admission.admit_external('reserve_slot','fixture','new-slot','a'*64)
                admission.admit_external('complete_slot','fixture','original-slot','a'*64)
                self.assertEqual(admission.observe()['completion'],1)
                original=admission.observe()
            finally:admission.close()
            recovered=SemanticAdmission(config,root,'OPEN_EXISTING')
            try:
                self.assertEqual(recovered.observe(),original)
                with self.assertRaises(OwnerFailure):recovered.admit_external('reserve_slot','fixture','another-slot','a'*64)
            finally:recovered.close()

    def test_two_gib_reserve_and_alias_observation_fail_closed(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();config,_=candidate(root,offline=False)
            admission=SemanticAdmission(config,root,'CREATE_NEW')
            try:
                before=admission.observe()
                with patch('companion_memory.persistence.semantic_admission.os.statvfs',return_value=SimpleNamespace(f_bavail=2097151,f_frsize=1024)):
                    with self.assertRaises(OwnerFailure):admission.admit_external('reserve_slot','fixture','blocked','a'*64)
                self.assertEqual(admission.observe(),before)
                (root/'alias').symlink_to(root/'semantic-admission.jsonl')
                with self.assertRaises(OwnerFailure):admission.checkpoint()
            finally:admission.close()
