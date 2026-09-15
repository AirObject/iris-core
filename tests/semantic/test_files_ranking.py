"""File-owner reader retirement and deterministic synthetic score boundaries.

These tests exercise file lifecycle and ranking only; artificial vectors and
scores make no claim about real supplier relevance or human-reviewed material.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from companion_memory.retrieval.semantic_files import VectorFiles
from companion_memory.retrieval.semantic_binary import VectorMember,vector_bytes,InvalidVectorFile
from companion_memory.retrieval.semantic_ranking import lexical_admitted,semantic_admitted,fuse
from companion_memory.retrieval.lexical import normalize_material
from tests.semantic.test_encoding import SPACE,GENERATION,ARTIFACT


def owner(root):
    return VectorFiles(root,file_limit_bytes=41943040,index_total_bytes=83886080,reader_limit=2)


class FileRankingTests(unittest.TestCase):
    def test_original_membership_native_evidence_and_changed_file_rejected(self):
        from companion_memory.retrieval.semantic_files import SealedGeneration
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'vectors';files=owner(path)
            member=VectorMember('object',1,1,1,ARTIFACT,vector_bytes((1.0,)*1024))
            sealed=files.seal(SPACE,GENERATION,1,[member],lambda:None)
            self.assertEqual(files.seal(SPACE,GENERATION,1,[member],lambda:None),sealed)
            for captured,members in ((2,[member]),(1,[]),(1,[member,member])):
                with self.assertRaises(InvalidVectorFile):files.seal(SPACE,GENERATION,captured,members,lambda:None)
            with self.assertRaises(TypeError):SealedGeneration()
            with files.reader(sealed),files.reader(sealed):
                with self.assertRaises(InvalidVectorFile):
                    with files.reader(sealed):pass
            self.assertTrue(files.close())
            files=owner(path)
            with self.assertRaises(InvalidVectorFile):
                with files.reader(sealed):pass
            sealed=files.verify(SPACE,GENERATION,lambda:None)
            with (path/GENERATION).open('r+b') as altered:
                altered.seek(4096+192);altered.write(b'\xff')
            with self.assertRaises(InvalidVectorFile):
                with files.reader(sealed):pass
            self.assertTrue(files.close())

    def test_reader_retirement_two_slots_and_new_interpreter_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'vectors';files=owner(path)
            member=VectorMember('object',1,1,1,ARTIFACT,vector_bytes((1.0,)*1024))
            first=files.seal(SPACE,GENERATION,1,[member],lambda:None)
            second_id='semantic-generation:'+'c'*64
            files.seal(SPACE,second_id,1,[member],lambda:None)
            with files.reader(first) as reader:
                self.assertEqual(reader.read(8),b'IRISVEC1')
                self.assertFalse(files.retire(GENERATION))
                with self.assertRaises(InvalidVectorFile):files.seal(SPACE,'semantic-generation:'+'d'*64,1,[member],lambda:None)
            self.assertTrue(files.close())
            command=[sys.executable,'-c',
                'from pathlib import Path; from tests.semantic.test_files_ranking import owner; from tests.semantic.test_encoding import SPACE,GENERATION; import sys,json; f=owner(Path(sys.argv[1])); s=f.verify(SPACE,GENERATION,lambda:None); print(json.dumps({"members":s.header.member_count,"digest":s.file_digest})); assert f.close()',str(path)]
            result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['digest'],first.file_digest)
            files=owner(path);self.assertTrue(files.retire(GENERATION));self.assertTrue(files.close())
            self.assertFalse((path/GENERATION).exists())

    def test_exact_thresholds_no_overlap_lexical_segment_and_rrf_order(self):
        material=lambda text:normalize_material(text,byte_limit=8192,term_limit=4096)
        self.assertFalse(semantic_admitted(.69,700000));self.assertTrue(semantic_admitted(.70,700000))
        self.assertFalse(lexical_admitted(material('abcdef'),material('abc'),600000))
        self.assertTrue(lexical_admitted(material('abcdef'),material('abcde'),600000))
        self.assertFalse(lexical_admitted(material('塔'),material('塔楼'),600000))
        self.assertTrue(lexical_admitted(material('塔'),material('塔 楼'),600000))
        self.assertEqual(fuse(('lexical',),('semantic',),(),rrf_k=60,combined_limit=128),('lexical','semantic'))
        self.assertEqual(fuse(('other','both'),('both',),('explicit',),rrf_k=60,combined_limit=128),('explicit','both','other'))
        self.assertEqual(fuse((),(),(),rrf_k=60,combined_limit=128),())
