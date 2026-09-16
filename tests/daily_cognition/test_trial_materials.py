"""Actual PNG pixels, exact reviewed text and unopened purpose slots."""
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from PIL import Image
from companion_memory.runtime.daily_trial_materials import write_materials,EVENTS,QUERIES,SLOTS

class TrialMaterialTests(unittest.TestCase):
    def test_closed_purpose_slots_do_not_issue_approval_or_fill_dynamic_documents(self):
        from companion_memory.runtime.daily_trial_package import purpose_slots
        slots=purpose_slots()
        self.assertEqual(len(slots),32);self.assertEqual(len({s['slot_id'] for s in slots}),32)
        self.assertEqual({role:sum(s['role']==role for s in slots) for role in SLOTS},dict(SLOTS))
        self.assertTrue(all(s['state']=='UNUSED' and s['attempt'] is None and s['usage'] is None for s in slots))
        documents=[s for s in slots if s['role']=='EMBEDDING_DOCUMENT']
        self.assertEqual(sum(s['selector']['origin']=='DIRECT_LEARNING' for s in documents),8)
        self.assertTrue(all(s['original_request'] is None for s in slots))

    def test_exact_pixels_closed_text_and_no_activation(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)/'materials';built=write_materials(root)
            self.assertEqual(built['attempt_limit'],32);self.assertEqual(built['attempts_used'],0)
            value=json.loads((root/'materials.json').read_text())
            self.assertEqual(value['authorization'],'NOT_APPROVED');self.assertEqual(value['reminder_sink'],'DISABLED')
            self.assertEqual(len(value['events']),10);self.assertEqual(tuple(value['queries']),QUERIES)
            self.assertEqual(value['events'][2]['body'],'本次展示计划在2030-01-02T10:00:00+08:00开始；希望提前600秒提醒。')
            self.assertEqual(len(EVENTS),10);self.assertEqual(sum(SLOTS.values()),32)
            for scene in ('A','B','C','D'):
                with Image.open(BytesIO((root/(scene+'.png')).read_bytes())) as image:
                    self.assertEqual(image.mode,'RGB');self.assertEqual(image.size,(256,256));self.assertEqual(image.info,{})
                    self.assertEqual(getattr(image,'n_frames',1),1)
                    colors=image.getcolors(65536)
                    if colors is None:raise AssertionError('Unexpected colors')
                    counts={color:count for count,color in colors}
                    if scene in ('A','B'):
                        self.assertEqual(counts[(255,0,0) if scene=='A' else (0,0,255)],80*96)
                        self.assertEqual(counts[(0,0,255) if scene=='A' else (255,0,0)],3209)
                    elif scene=='C':
                        self.assertEqual(counts[(0,255,0)],2*64*64)
                        self.assertEqual(image.getpixel((128,128)),(255,255,255));self.assertEqual(image.getpixel((128,129)),(255,255,0))
                        self.assertEqual(image.getpixel((64,223)),(255,255,0));self.assertEqual(image.getpixel((192,224)),(255,255,255))
                    else:self.assertEqual(counts,{(255,255,255):65536})
                    from hashlib import sha256
                    expected={'A':'18f9e1a7d79317b326d2e189515d8ebec5946333659f86476cd7865ec37f9754',
                        'B':'2f0aa1f47f7ede00863db121a513bedcdb78dbf082ae993738bc97c3373339ca',
                        'C':'50ab9d863e1bf18ef4535af0accc4523d34808b949305a5fb2bde31eb46996ff',
                        'D':'c1f7d702a80ae5e7dc52d62ef2577cbbb25c045d6dda3a49a872130518f48cc0'}
                    self.assertEqual(sha256(image.tobytes()).hexdigest(),expected[scene])
            with self.assertRaises(FileExistsError):write_materials(root)
