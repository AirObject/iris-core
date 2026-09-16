"""Explicit controlled-only approval for native mixed-purpose journal tests."""
from hashlib import sha256
import time
from companion_memory.configuration.daily_codec import candidate_values
from companion_memory.persistence.content_codec import encode_content
from companion_memory.runtime.daily_trial_authorization import DailyTrialAuthority
from companion_memory.runtime.daily_trial_package import canonical


def controlled_activation(host):
    if host.stored is None:raise AssertionError()
    binding={'format':'DAILY_TRIAL_AUTH_V1','package_id':'controlled-journal','config':{'database_id':host.stored.database_id,
        'instance_id':'instance','snapshot_id':host.stored.snapshot_id},'code_digest':'f'*64,
        'configuration_digest':sha256(canonical(candidate_values(host.configuration))).hexdigest(),
        'resources_digest':'f'*64,'materials_digest':'f'*64,'decision_ref':'controlled-fixture-only',
        'account_evidence_digest':'f'*64,'input_evidence_digest':'f'*64,'execution':'CONTROLLED','expires_at':time.time_ns()//1000+600000000}
    def select(request):
        from companion_memory.provider.daily_execution import DailyRequest
        if type(request) is DailyRequest:
            if request.binding.role!='LEARNING':raise AssertionError('Unexpected controlled role')
            return 'learning:0'
        return 'embedding_document:4' if request.description['payload']['purpose']=='DOCUMENT' else 'embedding_query:0'
    return DailyTrialAuthority(lambda value:dict(value)==binding and value['execution']=='CONTROLLED',select).activate(binding)
