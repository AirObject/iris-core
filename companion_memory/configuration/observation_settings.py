"""Native minimal management settings; no full configuration or storage capability."""
from dataclasses import dataclass
from .runtime_resolution import ConfigurationCandidate,runtime_snapshot_issue


@dataclass(frozen=True,slots=True,init=False)
class ObservationSettings:
    row_limit:int
    log_row_limit:int
    byte_limit:int
    timeout_seconds:float
    concurrency:int
    refresh_seconds:float
    def __init__(self):raise TypeError('Obtain settings from a complete configuration candidate.')


def observation_settings(candidate:ConfigurationCandidate) -> ObservationSettings:
    if runtime_snapshot_issue(candidate) is not None:raise ValueError('A complete native configuration is required.')
    value=object.__new__(ObservationSettings);s=candidate.runtime
    for key,item in {'log_row_limit':s.integer('logging.web_query_row_limit'),'row_limit':s.integer('management.observation_row_limit'),'byte_limit':s.integer('management.observation_max_bytes'),
        'timeout_seconds':s.integer('management.observation_timeout_ms')/1000,'concurrency':s.integer('management.observation_concurrency'),
        'refresh_seconds':s.integer('management.refresh_min_interval_ms')/1000}.items():object.__setattr__(value,key,item)
    return value


def content_observation_settings(candidate) -> ObservationSettings:
    """Project only observation limits from a complete native content candidate."""
    from .content_resolution import content_snapshot_issue
    if content_snapshot_issue(candidate) is not None: raise ValueError('Complete native content configuration required.')
    value = object.__new__(ObservationSettings)
    settings = candidate.runtime
    for key, item in {'log_row_limit': settings.integer('logging.web_query_row_limit'),
        'row_limit': settings.integer('management.observation_row_limit'), 'byte_limit': settings.integer('management.observation_max_bytes'),
        'timeout_seconds': settings.integer('management.observation_timeout_ms') / 1000,
        'concurrency': settings.integer('management.observation_concurrency'), 'refresh_seconds': settings.integer('management.refresh_min_interval_ms') / 1000}.items():
        object.__setattr__(value, key, item)
    return value
