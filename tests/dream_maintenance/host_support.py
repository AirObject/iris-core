"""Native dream host resources using the reviewed synthetic import fixture."""
from dataclasses import replace
import time
from typing import cast
from companion_memory.configuration.dream_resolution import resolve_dream_configuration,DreamConfigurationOk
from companion_memory.runtime.daily_host import DailyCognitionHost
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.credentials import CredentialResolver,CredentialLease,Available
from companion_memory.provider.values import Record
from tests.daily_cognition.test_host import make_host
from .configuration_support import inputs


def make_dream_host(root,port,credentials,*,with_self=False,connection_factory=None):
    # The daily fixture issues physical resources without opening its host.
    resources=make_host(root,port,credentials).resources
    if connection_factory is not None:resources=replace(resources,database=replace(resources.database,connect=connection_factory))
    if with_self:
        from .seed_support import self_material
        from hashlib import sha256
        from companion_memory.persistence.content_codec import encode_content
        from companion_memory.cognition.fixed_memory import FixedReviewAuthority
        claims=dict(resources.review.claims)|{'manifest_digest':sha256(encode_content(tuple(m['content_digest'] for m in self_material()),8192)).hexdigest()}
        resources=replace(resources,review=FixedReviewAuthority(lambda value:dict(value)==claims).grant(claims))
    parsed=resolve_dream_configuration(*inputs(root))
    if type(parsed) is not DreamConfigurationOk:raise AssertionError(parsed)
    def resolve(*args):
        lease=CredentialLease(b'synthetic-loopback-only');credentials.append(lease);return Available(lease)
    resolver=CredentialResolver(resolve)
    transports=dict(resources.transports)
    for setting in cast(tuple[Record,...],parsed.value.text.record('provider.transport')['roles']):
        role=cast(str,setting['role'])
        if role not in transports:
            transports[role]=ChatTransport.controlled_dream_loopback(setting,resolver,time.monotonic,port)
    host=DailyCognitionHost(parsed.value,replace(resources,transports=transports))
    host.configure_entry('entry','partition',('self',),({'kind':'REAL','context_id':None},))
    return host
