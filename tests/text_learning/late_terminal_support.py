"""Deterministic original-worker barrier after real controlled Chat decoding.

Only the shared Provider/transport observation clock advances. The original transport completes
once, but its worker cannot return until the actual UNKNOWN commit is observed.
No sleep duration is used as evidence of persistence or execution completion.
"""
import asyncio
from dataclasses import replace
import threading
import time
from unittest.mock import patch
from companion_memory.provider.generation_resources import ChatGenerationAdapter
from companion_memory.provider.service import ProviderService
from companion_memory.provider.ledger import LedgerBinding
from tests.text_learning.persona_terminal_support import PersonaScenario


class LateTerminalBarrier:
    """Hold one real adapter result, exposing its actual persisted unknown edge."""
    def __init__(self,scenario: PersonaScenario):
        self.scenario=scenario;self.entered=threading.Event();self.release=threading.Event()
        self.unknown=asyncio.Event();self.offset=0.0;self.job=None;self.evidence_command=None
        scenario.host.generation=replace(scenario.host.generation,monotonic=lambda:time.monotonic()+self.offset)
        scenario.host.generation.adapter.transport._monotonic=scenario.host.generation.monotonic
        original_invoke=ChatGenerationAdapter.invoke;original_unknown=ProviderService._record_unknown
        def invoke(adapter,request,role,cancellation,deadline):
            response=original_invoke(adapter,request,role,cancellation,deadline)
            self.entered.set()
            if not self.release.wait(20):raise AssertionError('The original result barrier was not released.')
            return response
        async def unknown(service,job,cause):
            await original_unknown(service,job,cause)
            if service is scenario.host.provider:
                self.job=job;self.unknown.set()
        original_mutate=LedgerBinding.mutate
        async def mutate(binding,kind,*args,**kwargs):
            result=await original_mutate(binding,kind,*args,**kwargs)
            if kind=='evidence' and binding is scenario.host.provider._ledger:self.evidence_command=result[1]
            return result
        self.mutate_patch=patch.object(LedgerBinding,'mutate',mutate)
        self.invoke_patch=patch.object(ChatGenerationAdapter,'invoke',invoke)
        self.unknown_patch=patch.object(ProviderService,'_record_unknown',unknown)

    def __enter__(self):
        self.invoke_patch.start();self.unknown_patch.start();self.mutate_patch.start();return self

    def __exit__(self,*args):
        self.release.set();self.offset=0;self.invoke_patch.stop();self.unknown_patch.stop();self.mutate_patch.stop()

    async def reach_unknown(self):
        assert await asyncio.to_thread(self.entered.wait,5),'Original Chat response was not decoded.'
        self.offset=100
        await asyncio.wait_for(self.unknown.wait(),5)
        self.offset=0
