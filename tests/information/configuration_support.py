"""Complete explicit configuration for owned temporary storage and local services.

Existing declarations retain their original provenance. The new registry carries
all information definitions; each test supplies the full supported value vector.
"""
from pathlib import Path
from companion_memory.configuration.information_schema import information_definitions, SUPPORTED_VALUES
from companion_memory.configuration.information_resolution import resolve_information_configuration, InformationConfigurationOk
from tests.configuration.content_support import inputs as content_inputs
from tests.runtime.configuration_support import registry


def inputs(root: Path, *, sink_mode: str = 'DISABLED'):
    base = content_inputs(root)
    information = {'registry': registry(list(information_definitions())),
                   'explicit_values': {key: dict(value) for key, value in SUPPORTED_VALUES.items()}}
    information['explicit_values']['goals.delivery']['sink_mode'] = sink_mode
    return (*base[:4], information, *base[4:])


def candidate(root: Path, *, sink_mode: str = 'DISABLED'):
    supplied = inputs(root, sink_mode=sink_mode)
    result = resolve_information_configuration(*supplied)
    if type(result) is not InformationConfigurationOk:
        raise RuntimeError('The explicit test configuration did not resolve: ' + repr(result))
    return result.value, supplied
