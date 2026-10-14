"""EF-10: one strict format repair per phase, shared deadline, no semantic retry."""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from iris import entry_profile as ep
from iris.models import Gateway, ModelConfig, ModelError, ModelReply
from fake_openai import completion
from test_entry_profile_memory_task import SimulationRepository, snapshot, new_topic, check


class Script:
    def __init__(self, repo, steps):
        self.repo, self.steps, self.now, self.requests = repo, iter(steps), 1000.0, []

    def monotonic(self):
        return self.now

    def chat(self, messages, purpose, **kwargs):
        assert not self.repo.in_transaction
        self.requests.append((purpose, copy.deepcopy(messages), kwargs))
        elapsed, output = next(self.steps)
        self.now += elapsed
        if isinstance(output, Exception):
            raise output
        return ModelReply(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False), 'stop', {})


def execute(steps, **kwargs):
    repo = SimulationRepository(snapshot())
    gateway = Script(repo, steps)
    result = ep.EntryProfileMemoryTask(repo, gateway, **kwargs).run('g')
    return repo, gateway, result


def generated():
    return {'operations': [new_topic()]}


def verdict():
    return check([new_topic()])


@pytest.mark.parametrize('defect', ['json', 'duplicate_key', 'missing_body', 'missing_source_field',
    'extra_field', 'boolean_importance', 'wrong_sources_type', 'wrong_top_type', 'saying_body'])
def test_generate_structure_repairs_once_before_semantic_validation(defect):
    bad = generated()
    if defect == 'json': bad = '{'
    elif defect == 'duplicate_key': bad = '{"operations":[],"operations":[]}'
    elif defect == 'missing_body': del bad['operations'][0]['body']
    elif defect == 'missing_source_field': del bad['operations'][0]['sources'][0]['via_memory']
    elif defect == 'extra_field': bad['operations'][0]['pinned'] = True
    elif defect == 'boolean_importance': bad['operations'][0]['importance'] = True
    elif defect == 'wrong_sources_type': bad['operations'][0]['sources'] = {}
    elif defect == 'wrong_top_type': bad = {'operations': {}}
    else: bad['operations'][0].update(category='saying', saying='留一步', meaning='先试一步', body='留一步：先试一步。')
    repo, gateway, result = execute([(20, bad), (30, generated()), (40, verdict())])
    assert result['status'] == 'committed', result
    assert len(repo.profiles) == 1 and len(gateway.requests) == 3
    assert [r[0] for r in gateway.requests] == ['consolidation_entry_profile_generate',
        'consolidation_entry_profile_generate_repair', 'consolidation_entry_profile_check']
    assert [r[2]['_deadline'] for r in gateway.requests] == [1180, 1180, 1230]
    assert gateway.requests[1][1][:2] == gateway.requests[0][1]
    assert gateway.requests[1][1][-2]['role'] == 'assistant'
    assert 'generate' in result['outputs'] and 'generate_repair' in result['outputs']
    assert result['calls'][0]['validation_error']


@pytest.mark.parametrize('defect', ['missing_violations', 'missing_groups', 'boolean_index',
    'string_flag', 'invalid_groups', 'unhashable_violation', 'missing_meaning_checks'])
def test_checker_structure_repairs_once(defect):
    bad = verdict(); row = bad['operations'][0]
    if defect == 'missing_violations': del row['violations']
    elif defect == 'missing_groups': del row['evidence_groups']
    elif defect == 'boolean_index': row['index'] = True
    elif defect == 'string_flag': row['supported'] = 'true'
    elif defect == 'invalid_groups': row['evidence_groups'] = [[0], [0]]
    elif defect == 'unhashable_violation': row['violations'] = [{}]
    else: del row['meaning_checks']
    repo, gateway, result = execute([(5, generated()), (30, bad), (40, verdict())])
    assert result['status'] == 'committed', result
    assert len(repo.profiles) == 1 and len(gateway.requests) == 3
    assert gateway.requests[-1][0] == 'consolidation_entry_profile_check_repair'
    assert [r[2]['_deadline'] for r in gateway.requests] == [1180, 1185, 1185]
    assert 'check_repair' in result['outputs']


@pytest.mark.parametrize('stage', ['generate', 'check'])
def test_second_invalid_result_is_invalid_output_and_never_commits(stage):
    prefix = [(1, generated())] if stage == 'check' else []
    repo, gateway, result = execute(prefix + [(2, '{'), (3, {'operations': 'still invalid'})])
    assert result['status'] == 'failed' and result['reason'] == 'invalid_output'
    assert repo.cursor == 0 and not repo.profiles
    assert len(gateway.requests) == len(prefix) + 2
    assert result['outputs'][stage] == '{' and stage + '_repair' in result['outputs']
    assert result['calls'][-1]['validation_error']


@pytest.mark.parametrize('stage', ['generate', 'check'])
def test_repair_at_deadline_keeps_all_state_unchanged(stage):
    prefix = [(1, generated())] if stage == 'check' else []
    output = verdict() if stage == 'check' else generated()
    repo, gateway, result = execute(prefix + [(120, '{'), (60, output)])
    assert result['reason'] == 'timeout' and result['status'] == 'failed'
    assert repo.cursor == 0 and not repo.profiles
    assert gateway.requests[-1][2]['_deadline'] == gateway.requests[-2][2]['_deadline']
    assert stage + '_repair' in result['outputs']


def test_invalid_parse_exhausting_deadline_cannot_start_repair(monkeypatch):
    repo = SimulationRepository(snapshot()); gateway = Script(repo, [(179, '{')])
    def parse(_):
        gateway.now += 2
        raise ValueError('invalid_output')
    monkeypatch.setattr(ep, 'json_object', parse)
    result = ep.EntryProfileMemoryTask(repo, gateway).run('g')
    assert result['reason'] == 'timeout' and len(gateway.requests) == 1
    assert repo.cursor == 0


def test_each_phase_has_one_repair_without_new_deadline():
    repo, gateway, result = execute([(20, '{'), (20, generated()), (20, '{'), (20, verdict())])
    assert result['status'] == 'committed'
    assert [r[2]['_deadline'] for r in gateway.requests] == [1180, 1180, 1220, 1220]
    assert len(repo.profiles) == 1


def test_semantic_rejection_is_not_a_format_repair():
    bad = verdict(); bad['operations'][0].update(supported=False, violations=['无依据'], reason='新增群体行为关系无依据')
    repo, gateway, result = execute([(1, generated()), (1, bad)])
    assert result['status'] == 'committed' and not repo.profiles
    assert len(gateway.requests) == 2 and result['removed'][0]['stage'] == 'model'
    op = new_topic(); op['body'] = '她常讨论装备'
    repo, gateway, result = execute([(1, {'operations': [op]})])
    assert not repo.profiles and len(gateway.requests) == 1
    assert result['removed'][0]['reason'] == 'gendered_pronoun'


@pytest.mark.parametrize('reason', ['rate_limited', 'timeout', 'content_refused'])
def test_provider_errors_do_not_trigger_json_repair(reason):
    repo, gateway, result = execute([(1, ModelError('retryable', 'private provider detail', reason=reason))])
    assert result['reason'] == reason and len(gateway.requests) == 1 and repo.cursor == 0
    assert 'private provider detail' not in json.dumps(result)


def test_http_retry_and_json_repair_share_the_same_deadline():
    repo = SimulationRepository(snapshot()); timer = SimpleNamespace(now=1000.0, timeouts=[], deadlines=[])
    steps = iter([(20, 503), (40, '{'), (10, generated()), (5, verdict())])
    def respond(request):
        elapsed, output = next(steps)
        timeout = request.extensions['timeout']['read']
        timer.timeouts.append(timeout); timer.deadlines.append(timer.now + timeout)
        timer.now += elapsed
        if output == 503: return httpx.Response(503, json={'error': {'code': 'InternalServerError'}})
        return httpx.Response(200, json=completion(output if isinstance(output, str) else json.dumps(output)))
    def sleep(seconds): timer.now += seconds
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway = Gateway({'chat': ModelConfig('https://example.invalid', '', 'fake')}, None, client,
            monotonic=lambda: timer.now, sleeper=sleep, jitter=lambda a,b: 0)
        try: result = ep.EntryProfileMemoryTask(repo, gateway).run('g')
        finally: gateway.close()
    assert result['status'] == 'committed', result
    assert timer.timeouts == [180, 158, 118, 180]
    assert timer.deadlines == [1180, 1180, 1180, 1252]


def test_shape_repair_cannot_reverse_a_completed_semantic_rejection():
    bad = verdict(); bad['operations'][0].update(supported=False, violations=['无依据'])
    del bad['operations'][0]['evidence_groups']
    repo, gateway, result = execute([(1, generated()), (1, bad), (1, verdict())])
    assert result['status'] == 'failed' and result['reason'] == 'invalid_output'
    assert result['calls'][-1]['validation_error'] == 'repair_changed_verdict'
    assert repo.cursor == 0 and not repo.profiles and len(gateway.requests) == 3


def test_corrected_output_rechecks_sqlite_sources_and_retains_purgeable_raw(store):
    from test_entry_profile_memory_live import message, RuleGateway, CLOCK
    from iris.memory_ops import purge_memory
    mid = message(store)
    class RepairGateway(RuleGateway):
        count = 0
        def chat(self, messages, purpose, **kwargs):
            self.count += 1
            assert not store._writer.in_transaction
            if self.count == 1:
                return ModelReply('{', 'stop', {})
            # The repair keeps the original request then appends the invalid answer.
            return super().chat(messages[:2], purpose.removesuffix('_repair'), **kwargs)
    gateway = RepairGateway(store)
    result = ep.EntryProfileEngine(store, gateway, clock=CLOCK).regenerate('group', expected_version=0)
    assert result['status'] == 'published' and gateway.count == 3
    with store.read() as conn:
        trace = json.loads(conn.execute('SELECT outputs_json FROM entry_profile_attempts').fetchone()[0])
        memory_id = conn.execute('SELECT id FROM memories').fetchone()[0]
    outputs=json.loads(trace['work'][0]['outputs_json'])
    assert outputs['observe'] == '{' and 'observe_repair' in outputs
    assert purge_memory(store, memory_id, 2, confirm=True)
    with store.read() as conn:
        outputs = conn.execute('SELECT outputs_json FROM entry_profile_attempts').fetchone()[0]
    assert '本群长期禁止广告' not in outputs and 'observe_repair' not in outputs


def test_repair_still_respects_maintenance_call_admission(store):
    from test_entry_profile_memory_live import message, CLOCK
    from iris.maintenance import Maintenance
    message(store)
    store.set_setting('consolidation', {'enabled': True, 'merge_enabled': False, 'conflict_enabled': False,
        'dependency_enabled': False, 'persona_enabled': False, 'goal_review_enabled': False,
        'entry_profile_enabled': True, 'person_summary_enabled': False, 'max_calls': 2})
    class BadGateway:
        calls = 0
        def chat(self, *args, **kwargs):
            assert not store._writer.in_transaction
            self.calls += 1
            return ModelReply('{', 'stop', {})
    gateway = BadGateway()
    maintenance = Maintenance(store, gateway=gateway, clock=CLOCK)
    run = maintenance.request()
    maintenance.run(run)
    assert gateway.calls == 2  # Each small request/repair consumes one actual call; no bulk-check reservation.
    with store.read() as conn:
        row = conn.execute('SELECT state,reason FROM entry_profile_attempts').fetchone()
        assert tuple(row) == ('failed', 'invalid_output')
        assert conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0
