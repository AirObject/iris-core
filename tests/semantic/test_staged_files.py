"""Real file-page interruption and sealing; database publication is separate."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
from companion_memory.retrieval.semantic_binary import VectorMember,vector_bytes,InvalidVectorFile,HEADER_BYTES,RECORD_BYTES
from tests.semantic.test_files_ranking import owner
from tests.semantic.test_encoding import SPACE,GENERATION,ARTIFACT


def members(count):
    return tuple(VectorMember(f'object:{n:04}',1,1,1,ARTIFACT,vector_bytes((float(n+1),)*1024)) for n in range(count))


def digest(values):
    return sha256(b'['+b','.join(value.binding_bytes() for value in values)+b']').hexdigest()


class StagedVectorFilesTests(unittest.TestCase):
    def test_original_pages_torn_tail_and_header_before_rename_recover(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)/'vectors';files=owner(root);values=members(9)
            first=files.stage_page(SPACE,GENERATION,1,0,values[:8],lambda:None)
            self.assertEqual(first.offset,HEADER_BYTES)
            self.assertEqual(first.page_digest,sha256(b''.join(m.encode() for m in values[:8])).hexdigest())
            files.confirm_written_page(first,lambda:None)
            # Simulate the OS retaining only a prefix of the originally written
            # second page. Recovery supplies that exact original database page.
            with (root/(GENERATION+'.building')).open('ab') as file:file.write(values[8].encode()[:17])
            self.assertTrue(files.close());files=owner(root)
            with self.assertRaises(InvalidVectorFile):files.confirm_written_page(first,lambda:None)
            second=files.stage_page(SPACE,GENERATION,1,1,values[8:],lambda:None)
            self.assertEqual(second.offset,HEADER_BYTES+8*RECORD_BYTES)
            files.confirm_written_page(second,lambda:None)
            with patch('companion_memory.retrieval.semantic_files.os.rename',side_effect=OSError('interrupted rename')):
                with self.assertRaises(OSError):files.seal_staged(SPACE,GENERATION,1,9,digest(values),lambda:None)
            self.assertFalse((root/GENERATION).exists())
            self.assertTrue(files.close());files=owner(root)
            sealed=files.seal_staged(SPACE,GENERATION,1,9,digest(values),lambda:None)
            self.assertEqual(sealed.header.member_count,9)
            self.assertEqual(sealed.file_bytes,HEADER_BYTES+9*RECORD_BYTES)
            self.assertEqual(files.seal_staged(SPACE,GENERATION,1,9,digest(values),lambda:None),sealed)
            self.assertFalse((root/(GENERATION+'.building')).exists())
            with files.reader(sealed):self.assertFalse(files.retire(GENERATION))
            self.assertTrue(files.retire(GENERATION));self.assertTrue(files.close())

    def test_conflicting_page_hole_count_digest_and_capture_are_rejected(self):
        with TemporaryDirectory() as directory:
            files=owner(Path(directory)/'vectors');values=members(9)
            with self.assertRaises(InvalidVectorFile):files.stage_page(SPACE,GENERATION,1,1,values[8:],lambda:None)
            files.stage_page(SPACE,GENERATION,1,0,values[:8],lambda:None)
            for page in (values[:1],values[1:9]):
                with self.assertRaises(InvalidVectorFile):files.stage_page(SPACE,GENERATION,1,0,page,lambda:None)
            with self.assertRaises(InvalidVectorFile):files.stage_page(SPACE,GENERATION,2,0,values[:8],lambda:None)
            with self.assertRaises(InvalidVectorFile):files.seal_staged(SPACE,GENERATION,1,9,digest(values),lambda:None)
            with self.assertRaises(InvalidVectorFile):files.seal_staged(SPACE,GENERATION,1,8,'0'*64,lambda:None)
            self.assertTrue(files.close())

    def test_zero_members_need_no_fake_page(self):
        with TemporaryDirectory() as directory:
            files=owner(Path(directory)/'vectors')
            with self.assertRaises(InvalidVectorFile):files.seal_staged(SPACE,GENERATION,0,0,'0'*64,lambda:None)
            sealed=files.seal_staged(SPACE,GENERATION,0,0,digest(()),lambda:None)
            self.assertEqual(sealed.file_bytes,4096);self.assertEqual(sealed.header.member_count,0)
            self.assertTrue(files.close())
