"""Independent ordinary and optimized interpreters enforce the same safe errors."""
import asyncio
import sys
import unittest


class ErrorProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_envelopes_do_not_depend_on_python_assertions(self):
        for options in ((),('-O',)):
            with self.subTest(optimized=bool(options)):
                child=await asyncio.create_subprocess_exec(sys.executable,*options,'-m','unittest','tests.runtime.error_boundary_cases','-v',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                async with asyncio.timeout(30):out,err=await child.communicate()
                self.assertEqual(child.returncode,0,(out+err).decode())
                self.assertIn('Ran 3 tests',err.decode())
