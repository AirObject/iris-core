"""Trusted daily observation assembly delegates each status read to its owner.

The immutable capability grants bounded management projections only. Entry
observation continues through the original entry-scoped content observer.
"""
from __future__ import annotations
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING
from companion_memory.persistence.schema import InvalidValue
from companion_memory.provider.daily_observation import DailyProviderObserver, observer
if TYPE_CHECKING:
    from .daily_host import DailyCognitionHost


@dataclass(frozen=True, slots=True, init=False)
class DailyObservations:
    host: DailyCognitionHost
    provider: DailyProviderObserver

    def __init__(self):
        raise TypeError('The native daily host issues owner observation.')

    async def read(self, scope: str, query: dict):
        if set(query) - ({'after','schedule_after'} if scope=='learning' else {'after'}) or type(query.get('after', '')) is not str or len(query.get('after', '').encode()) > 128:
            raise InvalidValue()
        if scope == 'learning':
            value = MappingProxyType({'schedule':await self.host.combination.schedule.observation(query.get('schedule_after', '')),
                'reasoning':await self.host.combination.reasoning.observation(query.get('after', ''))})
        elif scope == 'goal_dedup':
            value = await self.host.combination.goal_comparisons.observation(query.get('after', ''))
        elif scope in ('dream','maintenance','persona'):
            from companion_memory.dream.observation import observe
            value=await observe(self.host,scope,query.get('after',''))
        elif scope == 'media':
            if query:
                raise InvalidValue()
            value = await self.host.media.observation()
        else:
            raise InvalidValue()
        return MappingProxyType({scope: value, 'adapters': self.provider.origins(),
            'storage_execution': 'ACTUAL', 'candidate_origin': 'MODEL_OUTPUT_VALIDATED'})


def bind_daily_observations(host: DailyCognitionHost) -> DailyObservations:
    from .daily_host import DailyCognitionHost
    if type(host) is not DailyCognitionHost or host.provider is None or not host.provider.ready:
        raise InvalidValue()
    value = object.__new__(DailyObservations)
    object.__setattr__(value, 'host', host)
    object.__setattr__(value, 'provider', observer(host.provider))
    return value
