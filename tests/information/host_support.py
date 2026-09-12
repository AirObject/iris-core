"""Native owners with simulated models and explicit component/automatic timers.

Component fixtures control worker steps for transaction fault injection. Whole
host tests request automatic scheduling and execute the unmodified native host.
"""
from pathlib import Path
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaResources
from companion_memory.persistence import DatabaseResources
from companion_memory.persistence.resources import ConnectionFactory
from companion_memory.provider import SimulationAdapter
from companion_memory.runtime.information_host import InformationHost, InformationHostResources
from companion_memory.runtime.content_media import MediaPolicy
from tests.information.configuration_support import candidate
from tests.provider.support import success
import sqlite3


class ComponentHost(InformationHost):
    """Keep real owners but let component tests control maintenance admission."""
    def _start_local_workers(self) -> None:
        return None


def host(root: Path, connect: ConnectionFactory = sqlite3.connect, *, sink_mode: str = 'DISABLED', automatic: bool = False) -> InformationHost:
    root = root.resolve()
    configuration, supplied = candidate(root, sink_mode=sink_mode)
    resources = InformationHostResources(DatabaseResources('information-database', lambda database, path:
        database == 'information-database' and path == str(root / 'database/runtime.sqlite3'), connect=connect),
        MediaResources('information-media', lambda resource, database, path:
            resource == 'information-media' and database == 'information-database' and path == str(root / 'media')),
        'instance', 'configuration', supplied[5])
    implementation = InformationHost if automatic else ComponentHost
    return implementation(configuration, resources, SimulationAdapter((success(),)),
        SyntheticCandidateInput('literal_input', ('周末去北京看展',), 50), 'sample_learning', MediaPolicy('domain', 'sample_media', 'description'))


async def learn_one(value: InformationHost) -> str:
    """Create real memory via actual ingress and a marked synthetic candidate."""
    references = await learn_objects(value)
    if len(references) != 1: raise RuntimeError('Expected one real object.')
    return references[0]


async def learn_objects(value: InformationHost) -> tuple[str, ...]:
    """Apply the fixture's bounded synthetic candidate through real batch owners."""
    from companion_memory.persistence import Committed
    from companion_memory.information.records import record, text
    from tests.runtime.configuration_support import event
    registered = await value.register_entry('register', 'entry', 'host', 'sample_platform', 'conversation')
    if type(registered) is not Committed or value.runtime is None:
        raise RuntimeError('Expected real registered entry.')
    entry = value.runtime.bind_entry('entry')
    for ordinal in range(3):
        raw = event('input:' + str(ordinal)); raw['event_version'] = 2
        if type(await entry.accept_event('accept:' + str(ordinal), raw)) is not Committed:
            raise RuntimeError('Expected actual input persistence.')
    result = await entry.run_learning('learn')
    if type(result) is not Committed:
        raise RuntimeError('Expected confirmed synthetic candidate application: ' + repr(result))
    references = record(result.receipt.result)['object_refs']
    if type(references) is not tuple or not references:
        raise RuntimeError('Expected real applied objects.')
    return tuple(text(record(reference)['object_id']) for reference in references)
