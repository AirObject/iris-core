"""Full new database assembly with retained identity and explicit synthetic resources."""
import json
import asyncio
import time
import uuid
from datetime import datetime,timezone
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from typing import cast
from companion_memory.configuration import RuntimeConfigurationOk
from companion_memory.configuration.persistence import ConfigurationAssembly
from companion_memory.configuration.persistent_results import ConfigurationCommitted,ConfigurationUnconfirmed
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready
from companion_memory.provider import LedgerAssembly,ProviderService,ProviderResources,SimulationAdapter
from companion_memory.runtime import RuntimeAssembly,RuntimeService,IngressGrant
from companion_memory.runtime.results import Committed,RecoveryPending,RuntimeReady
from tests.runtime.configuration_support import candidate,event
from tests.runtime.participants import SyntheticParticipant
from tests.provider.support import Gate, success
from tests.persistence.support import Hooks
from companion_memory.logging_service import RuntimeLogWindow,create_logging_service,LoggingResources,LoggingOk


class Fixture:
    def __init__(self,root:Path,scenarios=(success(),),*,media=None,**configuration_changes):
        self.root=root.resolve();self.candidate,self.supplied=candidate(self.root,**configuration_changes)
        self.identity_path=self.root/'retained-identity.json'
        self.path=self.root/'database'/'runtime.sqlite3'
        if not self.identity_path.exists():self.identity_path.write_text(json.dumps({'identity':'runtime-database','path':str(self.path)}))
        self.config_assembly=ConfigurationAssembly();self.participant=SyntheticParticipant()
        self.runtime_assembly=RuntimeAssembly(self.participant,self.participant,media);self.provider_assembly=LedgerAssembly()
        self.storage=PersistenceService(self.config_assembly.repositories+self.runtime_assembly.repositories+self.provider_assembly.repositories,self.config_assembly.commands+self.runtime_assembly.commands+self.provider_assembly.commands+(self.participant.publication_command,)+( (media.command,) if media else ()))
        self.hooks=Hooks()
        self.resources=DatabaseResources('runtime-database',lambda identity,path:json.loads(self.identity_path.read_text())=={'identity':identity,'path':path},connect=self.hooks.connect)
        self.log_window=RuntimeLogWindow(self.candidate,'instance')
        self.logging=create_logging_service(observation_window=self.log_window)
        self.console=BytesIO()
        self.config_binding=None;self.runtime:RuntimeService | None=None
        self.provider=ProviderService(self.provider_assembly);self.adapter=SimulationAdapter(scenarios)
    async def initialize(self,mode='CREATE_NEW'):
        log=self.logging.initialize(self.candidate.foundation,LoggingResources(self.console,self.console,tuple((k,tuple(v)) for k,v in self.supplied[3].items()),lambda:str(uuid.uuid4()),lambda:datetime.now(timezone.utc),time.monotonic_ns))
        assert type(log) is LoggingOk,log
        ready=await self.storage.initialize(self.candidate.foundation,self.resources,mode)
        assert type(ready) is Ready,ready
        self.config_binding=self.config_assembly.bind(self.storage,'instance')
        configuration=await self.config_binding.persist_initial_configuration('original-config',self.candidate,actor='bootstrap',protected_directories=self.supplied[3])
        # Short runtime-deadline tests still need their original configuration
        # task to finish setup. Observe that retained task, including its full
        # SQLite reconstruction, rather than starting another reconstruction
        # after each short public wait. Production call deadlines stay intact.
        for _ in range(8):
            if type(configuration) is not ConfigurationUnconfirmed:break
            reference=configuration.reference
            retained=tuple(self.config_binding._tasks)
            if retained:
                assert len(retained)==1
                done,pending=await asyncio.wait(retained,timeout=5)
                assert done and not pending,'Original configuration setup remains in flight.'
                configuration=retained[0].result()
            else:
                await asyncio.sleep(.05)
                configuration=await self.config_binding.persist_initial_configuration('original-config',self.candidate,actor='bootstrap',protected_directories=self.supplied[3])
            if type(configuration) is ConfigurationCommitted:
                receipt=configuration.receipt
                assert (receipt.identity,receipt.command_version,receipt.fingerprint_version,receipt.fingerprint)==(
                    reference.identity,reference.command_version,reference.fingerprint_version,reference.fingerprint)
        assert type(configuration) is ConfigurationCommitted and configuration.configuration is not None,configuration
        self.runtime=RuntimeService(self.runtime_assembly,self.storage,configuration.configuration,'instance')
        binding=self.provider_assembly.bind(self.storage,self.candidate.foundation)
        await self.provider.initialize(self.candidate.foundation,binding,ProviderResources(self.runtime.provider_gate,self.adapter))
        self.runtime.attach_provider(self.provider)
        logger=self.logging.get_logger('runtime');assert type(logger) is LoggingOk
        self.runtime.attach_logger(logger.value)
        ready=await self.runtime.initialize_runtime()
        # Fixture setup may span several bounded local recovery pages. Wait for
        # actual owners before continuing; never extend the tested deadlines.
        for _ in range(8):
            if type(ready) is not RecoveryPending:break
            assert ready.stage=='RUNTIME' and ready.reason in ('DEADLINE_EXCEEDED','OWNER_ACTIVE','COMMIT_UNCONFIRMED'),ready
            retained=tuple(self.runtime._jobs)
            if retained:
                done,pending=await asyncio.wait(retained,timeout=5)
                assert done and not pending,'Original runtime setup remains in flight.'
            ready=await self.runtime.initialize_runtime()
        assert type(ready) is RuntimeReady,ready
        return self.runtime
    async def entry(self,name='sample_entry'):
        assert self.runtime is not None
        registered=await self.runtime.register_entry('register:'+name,{'instance_id':'instance','host_id':'host','platform_id':'sample_platform','external_entry_id':name})
        assert type(registered) is Committed,registered
        eid=cast(str,cast(MappingProxyType,registered.receipt.result)['entry_id'])
        port=await self.runtime.bind_ingress(IngressGrant('instance','host','sample_platform',eid,'actor'))
        return eid,port
    async def close(self):
        if self.runtime:await self.runtime.close()
        await self.provider.close()
        if self.runtime:await self.runtime.close()
        if self.config_binding:self.config_binding.close()
        await self.storage.close()
        if self.config_binding:self.config_binding.close()
        self.logging.close();self.log_window.close()
