"""Explicit native text-host resource assembly for bounded provider qualification.

This module never chooses account prices or imports a user's key. The caller
must supply a fully resolved uniform configuration and a native credential
resolver. Offline loopback is an explicit separate construction argument.
"""
from __future__ import annotations
from datetime import datetime,timezone
from pathlib import Path
import time
import uuid
from typing import cast
from companion_memory.configuration.text_resolution import TextConfigurationCandidate
from companion_memory.cognition.text_resources import output_schema
from companion_memory.media.service import MediaResources
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.persistence import DatabaseResources
from companion_memory.provider.chat_protocol import ChatBinding
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.wire_evidence import WireEvidence
from companion_memory.provider.credentials import CredentialResolver
from companion_memory.provider.generation_resources import ChatGenerationAdapter,RealGenerationResources
from companion_memory.provider.values import Record,as_record
from companion_memory.runtime.content_gate import ContentGate
from companion_memory.runtime.text_host import TextHost,TextHostResources


def assemble(configuration: TextConfigurationCandidate,root: Path,platform: str,
             directories: dict[str,list[str]],resolver: CredentialResolver,*,loopback_port: int|None=None,
             evidence: WireEvidence|None=None) -> TextHost:
    """Create a host with distinct database/instance identities, without sending.

    The actual transport uses only configuration's fixed HTTPS endpoint. An
    explicit loopback port selects the existing deterministic protocol facility
    and must never receive the real credential resolver.
    """
    if platform not in ('macos','linux'):raise ValueError('Unsupported trial platform.')
    gate=ContentGate(1)
    prefix='deepseek-' if configuration.text.record('provider.generation')['model_id']=='deepseek-flash' else ''
    database_id=prefix+'text-trial-'+platform;instance_id=prefix+'trial-'+platform
    transport_config=as_record(cast(Record,configuration.text.record('provider.transport')))
    transport=(ChatTransport(transport_config,resolver,time.monotonic,evidence=evidence) if loopback_port is None else
               ChatTransport.controlled_loopback(transport_config,resolver,time.monotonic,loopback_port))
    generation=configuration.text.record('provider.generation');bindings=[]
    for role,name in (('LEARNING','text_learning'),('PERSONA','initial_persona')):
        selected=generation if role=='LEARNING' else configuration.text.record('self_model.initial_persona')
        bindings.append(ChatBinding(cast(str,generation['model_id']),cast(tuple[str,...],generation['expected_reported_models']),
            cast(str|None,generation['resolved_model_id']),cast(str,selected['schema_ref']),cast(str,selected['schema_digest']),name,output_schema(role)))
    adapter=ChatGenerationAdapter(transport,*bindings)
    resources=RealGenerationResources(gate.binding,adapter,resolver,time.monotonic,lambda:datetime.now(timezone.utc),lambda:str(uuid.uuid4()),lambda _:None)
    database=DatabaseResources(database_id,lambda identity,path:identity==database_id and path==str(root/'database'/'runtime.sqlite3'))
    media_id=prefix+'trial-media-'+platform
    media=MediaResources(media_id,lambda rid,did,path:(rid,did,path)==(media_id,database_id,str(root/'media')))
    host_resources=TextHostResources(database,media,instance_id,prefix+'trial-configuration',directories,
        InitialSelfBinding('local-trial-operator',prefix+'trial-self-'+platform,'合成自我苔灯','SYNTHETIC_FIXTURE'))
    return TextHost(configuration,host_resources,resources,gate)
