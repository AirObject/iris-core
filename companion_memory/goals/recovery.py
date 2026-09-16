"""Read-only goal, attribution, frozen-task and reminder relationship checks.

The native host terminalizes unresolved attempts only after these validations;
this verifier never sends, merges, creates goals or rewrites damaged records.
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from companion_memory.persistence import Value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.records import integer, identity, text
from .repository import goal_layouts
if TYPE_CHECKING:
    from .service import GoalsService


def require(condition: bool) -> None:
    if not condition: raise OwnerFailure('STORAGE_FAILED', 'goal', 'INTEGRITY_FAILURE')


async def verify_goals(owner: GoalsService) -> None:
    """Bounded canonical rows and exact child counts share recovery's deadline."""
    from .service import signature
    from .inputs import valid_world_scope
    rows = owner._records
    require(integer((await rows.rows.read('open_count', {}))[0]['count']) <= 1000)
    require((await rows.rows.read('recovery_invalid_candidates', {}))[0]['count'] == 0)
    for layout in goal_layouts(owner.daily_format):
        after: dict[str, Value] = {'after_' + key: '' for key in layout.keys}
        while page := await rows.rows.read(layout.name + '_recovery_page', after):
            for raw in page:
                value = rows.unpack(layout.name, raw)
                name = layout.name
                if name == 'goal':
                    require(valid_world_scope(text(value['world_scope'])))
                    require(raw['signature'] == signature(value) and integer(value['created_at']) <= integer(value['updated_at']))
                    require(value['deadline'] is None or value['route_id'] is not None and value['reminder_lead_seconds'] is not None)
                    counts = (await rows.rows.read('recovery_child_counts', {'goal_id': value['goal_id']}))[0]
                    require(counts['sources'] == value['source_count'] and integer(counts['sources']) >= 1
                        and counts['aliases'] == value['alias_count'] and counts['tasks'] == 1)
                    if value['canonical_id'] != value['goal_id']:
                        alias = await rows.read('alias', {'alias_id': value['goal_id']})
                        require(alias is not None and alias['canonical_id'] == value['canonical_id'] and value['dedup_state'] in (('EXACT_MERGED','SEMANTIC_MERGED') if owner.daily_format else ('EXACT_MERGED',)))
                elif name == 'source':
                    require(await rows.read('goal', {'goal_id': value['goal_id']}) is not None)
                elif name == 'alias':
                    source = await rows.read('goal', {'goal_id': value['alias_id']})
                    target = await rows.read('goal', {'goal_id': value['canonical_id']})
                    require(source is not None and target is not None and source['canonical_id'] == target['goal_id']
                        and target['canonical_id'] == target['goal_id'] and source['goal_id'] != target['goal_id'])
                elif name == 'dedup_task':
                    require(value['config_snapshot_id'] == owner.binding.config_snapshot_id and value['task_id'] == identity('goal_dedup', value['goal_id']))
                    goal = await rows.read('goal', {'goal_id': value['goal_id']})
                    require(goal is not None and goal['dedup_state'] == value['status'])
                    require((await rows.rows.read('recovery_candidate_count', {'task_id': value['task_id']}))[0]['count'] == value['candidate_count'])
                    require(value['status'] != 'RUNNING' or value['owner_id'] is not None and value['started_at'] is not None)
                elif name == 'reminder_plan':
                    require(value['config_snapshot_id'] == owner.binding.config_snapshot_id
                        and await rows.read('goal', {'goal_id': value['goal_id']}) is not None
                        and value['plan_id'] == identity('reminder', value['goal_id'], value['deadline_revision'], value['kind']))
                    if value['delivery_id'] is not None:
                        attempt = await rows.read('attempt', {'delivery_id': value['delivery_id']})
                        require(attempt is not None and attempt['plan_id'] == value['plan_id'])
                    else: require(value['status'] not in ('ATTEMPTING', 'ACKNOWLEDGED', 'UNKNOWN', 'FAILED'))
                elif name == 'attempt':
                    plan = await rows.read('reminder_plan', {'plan_id': value['plan_id']})
                    state = 'ATTEMPTING' if value['state'] == 'REGISTERED' else 'UNSENT_UNAVAILABLE' if value['state'] == 'NOT_SENT' else value['state']
                    require(plan is not None and plan['delivery_id'] == value['delivery_id'] and plan['status'] == state)
                    require(value['delivery_id'] == identity('delivery', value['plan_id'], value['operation_key']))
                    require((value['finished_at'] is None) == (value['state'] == 'REGISTERED'))
                    require(value['finished_at'] is None or integer(value['finished_at']) >= integer(value['started_at']))
                after = {'after_' + key: raw[key] for key in layout.keys}
