"""Verify actual image bytes, pixel geometry and distinct complete wire encoders."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import base64
import json
import struct
import tempfile
import unittest
from PIL import Image
from companion_memory.media.image_validation import ImageLimits,CheckedImage,ImageRejected,validate_image
from companion_memory.provider.image_protocol import IMAGE_SYSTEM,IMAGE_USER,encode_deepseek_image,encode_minimax_image,decode_image_output
from companion_memory.provider.values import InvalidData
from .materials import png_bytes,pixel,_chunk,write_materials

LIMITS=ImageLimits(1048576,2048,4194304,1)


class ImageTests(unittest.TestCase):
    def test_png_files_match_every_approved_pixel_and_have_no_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            write_materials(Path(directory))
            for scene in 'ABCD':
                raw=(Path(directory)/(scene+'.png')).read_bytes();checked=validate_image(raw,LIMITS)
                self.assertIsInstance(checked,CheckedImage)
                with Image.open(BytesIO(raw)) as image:
                    self.assertEqual(image.mode,'RGB');self.assertEqual(image.info,{})
                    self.assertEqual(image.tobytes(),bytes(c for y in range(256) for x in range(256) for c in pixel(scene,x,y)))
            self.assertEqual(pixel('C',128,129),(255,255,0))
            self.assertEqual(pixel('C',64,224),(255,255,255))
            self.assertEqual(pixel('C',128,223),(255,255,0))

    def test_whole_jpeg_and_complete_base64_wire(self):
        buffer=BytesIO()
        with Image.new('RGB',(16,16),'white') as image:image.save(buffer,format='JPEG')
        for raw in (buffer.getvalue(),png_bytes('A')):
            image=validate_image(raw,LIMITS);self.assertIsInstance(image,CheckedImage)
            if type(image) is not CheckedImage:raise AssertionError(image)
            deep=json.loads(encode_deepseek_image(image,system=IMAGE_SYSTEM,text=IMAGE_USER))
            mini=json.loads(encode_minimax_image(image,system=IMAGE_SYSTEM,text=IMAGE_USER))
            self.assertEqual(set(deep),{'model','messages','max_tokens','stream','thinking','response_format'})
            self.assertEqual(set(mini),{'model','messages','max_completion_tokens','stream','thinking','reasoning_split'})
            self.assertIn('json',deep['messages'][0]['content'].lower())
            self.assertEqual(mini['messages'][0]['content'],IMAGE_SYSTEM)
            url=deep['messages'][1]['content'][1]['image_url']['url']
            self.assertEqual(base64.b64decode(url.split(',',1)[1],validate=True),raw)
            self.assertNotIn('detail',deep['messages'][1]['content'][1]['image_url'])

    def test_exact_original_byte_limit_is_encoded_without_truncation(self):
        original=png_bytes('D');extra=1048576-len(original)-12
        raw=original[:-12]+_chunk(b'vpAg',b'x'*extra)+original[-12:]
        self.assertEqual(len(raw),1048576)
        image=validate_image(raw,LIMITS);self.assertIsInstance(image,CheckedImage)
        if type(image) is not CheckedImage:raise AssertionError(image)
        wire=encode_deepseek_image(image,system=IMAGE_SYSTEM,text=IMAGE_USER)
        self.assertLessEqual(len(wire),2097152)
        self.assertEqual(sha256(base64.b64decode(json.loads(wire)['messages'][1]['content'][1]['image_url']['url'].split(',')[1])).hexdigest(),sha256(raw).hexdigest())
        self.assertEqual(validate_image(raw+b'x',LIMITS),ImageRejected('IMAGE_LIMIT_EXCEEDED'))
        print({'actual_image_bytes':len(raw),'wire_bytes':len(wire)})

    def test_wrong_format_truncation_size_and_animation_do_not_become_refusal(self):
        self.assertEqual(validate_image(b'GIF89a',LIMITS),ImageRejected('INPUT_FORMAT_UNSUPPORTED'))
        self.assertEqual(validate_image(png_bytes('A')[:-20],LIMITS),ImageRejected('CONTENT_CORRUPT'))
        with Image.new('RGB',(2049,1)) as image:
            buffer=BytesIO();image.save(buffer,format='PNG')
        self.assertEqual(validate_image(buffer.getvalue(),LIMITS),ImageRejected('IMAGE_LIMIT_EXCEEDED'))
        with Image.new('RGB',(2,2),'red') as first,Image.new('RGB',(2,2),'blue') as second:
            buffer=BytesIO();first.save(buffer,format='PNG',save_all=True,append_images=[second])
        self.assertEqual(validate_image(buffer.getvalue(),LIMITS),ImageRejected('IMAGE_LIMIT_EXCEEDED'))

    def test_image_output_has_a_separate_byte_boundary(self):
        self.assertEqual(decode_image_output(b'{"schema_version":1,"text":""}')['text'],'')
        for value in ({'schema_version':True,'text':'x'},{'schema_version':1,'text':'字'*171},
                {'schema_version':1,'text':'x','extra':None}):
            with self.assertRaises(InvalidData):decode_image_output(json.dumps(value,ensure_ascii=False).encode())
