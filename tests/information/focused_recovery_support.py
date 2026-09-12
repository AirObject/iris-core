"""Actual retained goals with a clearly synthetic focus publication participant."""
from contextlib import closing
from collections.abc import Callable
from pathlib import Path
import sqlite3
import time
from typing import TypedDict
from companion_memory.persistence import Found, Committed
from companion_memory.persistence.resources import ConnectionFactory
from companion_memory.information.records import record, text, identity, integer
from tests.information.host_support import host, ComponentHost
from tests.information.test_reminders import ready_goal
from tests.runtime.test_content_focus import ExplicitPublication


def focused_host(root: Path, connect: ConnectionFactory = sqlite3.connect) -> ComponentHost:
    template = host(root, connect, sink_mode='TEST_HTTP')
    return ComponentHost(template.configuration, template.resources, template.adapter,
        template.candidates, template.learning_profile, template.media_policy, ExplicitPublication())


async def prepare(root: Path, mode: str, *, goal_count: int = 2, before_write: Callable[[], None] = lambda: None) -> None:
    if not 2 <= goal_count <= 100: raise ValueError('Bounded local recovery profile required.')
    before_write()
    h = focused_host(root)
    try:
        if type(await h.initialize('CREATE_NEW')) is not Found: raise RuntimeError('Expected actual host initialization.')
        port, _ = await ready_goal(h)
        if h.goals is None or h.runtime is None: raise RuntimeError('Expected actual goal/runtime owners.')
        plans = await h.goals.due_plans(time.time_ns() // 1000)
        registered, _ = await h.management.register_reminder(port, text(plans[0]['plan_id']), integer(plans[0]['revision']), 'register-only')
        if type(registered) is not Committed: raise RuntimeError('Expected registered actual reminder.')
        for ordinal in range(goal_count - 1):
            before_write()
            injected = await port.execute('goal_inject_external', 'unfinished:' + str(ordinal), {'content': '另一件独立本地任务' + str(ordinal), 'subject_ids': (), 'world_scope': 'REAL',
                'deadline': None, 'reminder_lead_seconds': None, 'route_id': None, 'source_id': 'external'})
            if type(injected) is not Committed: raise RuntimeError('Expected actual second goal.')
            goal_id = text(record(record(record(injected.receipt.result)['facts'])['goals'])['object_id'])
            claimed = await port.execute('goal_dedup_claim', 'unfinished-claim:' + str(ordinal), {'task_id': identity('goal_dedup', goal_id), 'expected_revision': 1, 'owner_id': 'retained-worker'})
            if type(claimed) is not Committed: raise RuntimeError('Expected retained actual task.')
        before_write()
        if mode == 'NORMAL': return
        if mode == 'DREAM_FOCUSED':
            entered = await h.runtime.focus.bind('retained-focus').enter_focus('enter', 1)
        else:
            h.runtime.gate.close_ordinary()
            entered = await h.runtime.execute('change_content_mode', 'enter', {'action': 'ENTER', 'expected_epoch': 1, 'run_id': 'retained-focus', 'publication_id': None})
        if type(entered) is not Committed: raise RuntimeError('Expected actual persistent focus: ' + repr(entered))
    finally:
        if not await h.close(): raise RuntimeError('Expected actual close.')


class RecoveryFacts(TypedDict):
    tasks: list[tuple[str, str, int]]
    attempts: list[tuple[str, str, int]]
    mode: list[tuple[str, int, str]]
    receipts: list[tuple[str, str, str]]
    audits: int


def facts(root: Path) -> RecoveryFacts:
    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
        return {'tasks': connection.execute('SELECT task_id,status,revision FROM goals_dedup_task ORDER BY task_id').fetchall(),
            'attempts': connection.execute('SELECT delivery_id,state,revision FROM goals_attempt').fetchall(),
            'mode': connection.execute('SELECT state,epoch,run_id FROM runtime_content_mode').fetchall(),
            'receipts': connection.execute('SELECT operation_kind,operation_key,commit_id FROM operation_receipts ORDER BY operation_kind,operation_key').fetchall(),
            'audits': connection.execute('SELECT count(*) FROM audit_records').fetchone()[0]}
