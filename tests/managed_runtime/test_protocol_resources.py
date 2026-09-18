"""Independent JSON Schema validation of generated protocols and real responses."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from jsonschema import Draft202012Validator
from protocol.generate import generate
from companion_memory.management.communication_http_schema import HTTP_ROUTES
from companion_memory.management.communication_protocol import SCHEMAS
from companion_memory.management.managed_host_http import ROUTES
from .test_communication_schema import maximum

ROOT=Path(__file__).resolve().parents[2]


def validate_http(path: str, response: dict):
    document=json.loads((ROOT/'protocol/http.json').read_text())
    value=document['paths'][path]['post']['responses']['200']['content']['application/json']['schema']
    Draft202012Validator(value|{'components':document['components']}).validate(response)


class ProtocolResourcesTests(unittest.TestCase):
    def test_generated_resources_are_exact_and_all_declared_schemas_are_valid(self):
        with TemporaryDirectory() as directory:
            generate(Path(directory))
            for name in ('http.json','ws.json'):
                self.assertEqual((Path(directory)/name).read_bytes(),(ROOT/'protocol'/name).read_bytes(),name)
        http=json.loads((ROOT/'protocol/http.json').read_text());ws=json.loads((ROOT/'protocol/ws.json').read_text())
        for schema in http['components']['schemas'].values(): Draft202012Validator.check_schema(schema)
        for path in http['paths'].values():
            for content in path['post']['requestBody']['content'].values(): Draft202012Validator.check_schema(content['schema'])
        Draft202012Validator.check_schema(ws)
        self.assertTrue({route.path for route in HTTP_ROUTES}<={*http['paths']})
        self.assertTrue({'/api/host/'+name for name in ROUTES}<={*http['paths']})
        for name,schema in SCHEMAS.items():
            Draft202012Validator(ws['$defs'][name]).validate(maximum(schema))
        for filename,path in (('event.json','accept'),('query.json','memory/search')):
            supplied={'entry_id':'entry','input':json.loads((ROOT/'clients/examples'/filename).read_text())}
            shape=http['paths']['/api/host/'+path]['post']['requestBody']['content']['application/json']['schema']
            Draft202012Validator(shape|{'components':http['components']}).validate(supplied)
        print({'openapi_routes':len(http['paths']),'complete_native_result_schemas':len(http['components']['schemas']),
            'websocket_messages':len(SCHEMAS),'generator':'BYTE_IDENTICAL','validator':'Draft202012Validator'})


class ProtocolClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_executable_http_client_and_real_public_observations(self):
        import asyncio
        import os
        import sys
        import time
        from clients.iris_client import IrisClient
        from tests.daily_cognition.test_reasoning import responses
        from tests.daily_cognition.test_initial_persona_host import persona
        from .communication_live_support import ready_application
        with TemporaryDirectory() as directory,responses((persona,)) as (provider_port,requests,failures):
            app,http=await ready_application(self,Path(directory)/'instance',provider_port,port=18189)
            try:
                _,token=await app.identity.create_token('schema-client','host',('entry',),('accept','confirm','query','prepare','state_read','goal_read'),time.time_ns()//1000+120000000)
                assert token is not None
                client=IrisClient('http://127.0.0.1:18189',token)
                child=await asyncio.create_subprocess_exec(sys.executable,'-m','clients.http_client','--origin',client.origin,
                    '--operation','capabilities',env=os.environ|{'IRIS_HOST_TOKEN':token},stdout=asyncio.subprocess.PIPE)
                stdout,_=await child.communicate()
                self.assertEqual(child.returncode,0);validate_http('/api/host/capabilities',json.loads(stdout))
                query=json.loads((ROOT/'clients/examples/query.json').read_text())
                for path,body in (('state',{}),('goals',{}),('memory/search',query),
                        ('prepare',query|{'request_key':'schema-prepare','participant_ids':[],'situation':'合成协议验证'})):
                    response=await asyncio.to_thread(client.request,'/api/host/'+path,{'entry_id':'entry','input':body})
                    validate_http('/api/host/'+path,response)
                    self.assertEqual(response['outcome'],'OBSERVED',response)
                    await asyncio.sleep(.05)
                self.assertEqual((len(requests),failures),(1,[]))
                print({'actual_http_cli':'EXIT_0','public_query_state_goal_responses':'SCHEMA_VALID','supplier_requests':0})
            finally:
                while not await http.close(): await asyncio.sleep(.05)
                while not await app.business.close(): await asyncio.sleep(.05)
                await app.bootstrap.close()
