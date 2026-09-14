"""Closed durable generation records with explicit persisted configuration IDs.

The text ledger selects these schemas at assembly time. Simulation readers do
not accept them. Every body, safe observation and monetary item is revalidated
before publication or arithmetic; physical identity checks remain in the ledger.
"""
from typing import cast
from .stored_schema import exact, integer, identifier, identifiers, enum, timestamp, checksum, boolean, cause, TERMINALS, USAGE_FIELDS
from .values import Record, InvalidData, as_record, freeze
from .token_costs import rounded_cost, add, quantity, InvalidAmount
from companion_memory.configuration.text_schema import ACCOUNT, PROFILE
from companion_memory.persistence.schema import freeze_value, encode_value, InvalidValue

SCHEMAS = {
    'requests': 'caller_module caller_scope extension_id operation_key capability task_role result_owner profile_id account_id created_at updated_at format_version fingerprint_version attribution source configuration_origin config_snapshot_id profile_revision price_revision execution_evidence fingerprint phase outcome first_error attempt_count ever_unknown handoff_id',
    'attempts': 'request_id ordinal state logical_outcome account_id profile_id capability wire_protocol execution_owner_id created_at updated_at adapter_duration_ms handoff_id confirmed_started ever_unknown first_error terminal_error usage result_fingerprint evidence_revision',
    'budget_windows': 'account_id window_id policy attempt_count known_subtotal_atoms held_atoms risk_state format_version quota_reserved quota_known quota_held',
    'reservations': 'attempt_id account_id budget_id reserved_atoms known_subtotal_atoms held_atoms known_cost_atoms cost_complete format_version quota_reserved quota_known quota_held',
    'cost_items': 'attempt_id item cost_atoms evidence_revision source unit known_subtotal_atoms cost_complete format_version quantity price_numerator price_denominator',
    'handoffs': 'request_id owner_id checksum source artifact_id format_version created_at',
}
RAW_FIELDS = 'prompt_tokens completion_tokens total_tokens cached_tokens reasoning_tokens provisioned_input_tokens provisioned_output_tokens'
USAGE_NAMES = 'fields raw_usage source coverage cost_complete known_cost_atoms known_subtotal_atoms held_atoms estimated_cost_atoms reported_cost_atoms price_revision items valid cost_disagreement format_version currency atom_scale billing_mode quota_known quota_held'


def account(value: object) -> Record:
    """Bound the entire closed account declaration before using its policy."""
    result = as_record(freeze(value, 4096, owned=True))
    try:
        encode_value(freeze_value(ACCOUNT, result, owned=True), 4096)
    except InvalidValue:
        raise InvalidData() from None
    if result['attempt_limit'] not in (14,16):raise InvalidData()
    if result['billing_mode']=='USAGE_ONLY_TRIAL':
        price=as_record(result['price'])
        if result['cost_limit_atoms']!=0 or result['quota'] is not None or any(price[k] is not None for k in ('input_atoms_per_million','cached_atoms_per_million','output_atoms_per_million','per_attempt_money_bound')):raise InvalidData()
    elif result['cost_limit_atoms']==0:raise InvalidData()
    return result


def profile(value: object) -> Record:
    result = as_record(freeze(value, 2048, owned=True))
    try:
        encode_value(freeze_value(PROFILE, result, owned=True), 2048)
    except InvalidValue:
        raise InvalidData() from None
    protocol={'ark-code-latest':'OPENAI_CHAT_COMPLETIONS','MiniMax-M3':'MINIMAX_CHAT_JSON_V1','deepseek-flash':'DEEPSEEK_CHAT_JSON_V1'}[str(result['model_id'])]
    if (result['wire_protocol']!=protocol or (result['billing_mode']=='USAGE_ONLY_TRIAL')!=(result['model_id']=='MiniMax-M3')
            or result['model_id']=='deepseek-flash' and result['billing_mode']!='TOKEN_METERED'):raise InvalidData()
    if result['dimensions'] is not None or result['space_id'] is not None:
        raise InvalidData()
    return result


def validate_usage(value: object, *, not_sent: bool = False) -> Record:
    """Check complete UsageV2, preserving unknown quantities and rational items."""
    item = exact(freeze(value, 4096, owned=True), USAGE_NAMES)
    if item['format_version']==4:
        from .deepseek_usage import validate as validate_deepseek
        return validate_deepseek(item,not_sent=not_sent)
    if item['format_version']==3:
        from .usage_only import validate_usage as validate_trial
        return validate_trial(item,not_sent=not_sent)
    integer(item['format_version'], minimum=2, maximum=2)
    enum(item['currency'], ('CNY', 'USD')); integer(item['atom_scale'], minimum=1000000, maximum=1000000)
    enum(item['billing_mode'], ('TOKEN_METERED', 'SUBSCRIPTION'))
    fields = exact(item['fields'], ' '.join((*USAGE_FIELDS, 'total_tokens')))
    raw = exact(item['raw_usage'], RAW_FIELDS)
    for name in fields:
        integer(fields[name], True)
    for name in raw:
        integer(raw[name], True)
    for name in ('cache_write_tokens', 'input_items', 'embedding_dimensions', 'rerank_candidates', 'media_bytes', 'media_duration_ms'):
        if fields[name] is not None: raise InvalidData()
    for target, source in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
            ('total_tokens', 'total_tokens'), ('cache_read_tokens', 'cached_tokens'), ('reasoning_tokens', 'reasoning_tokens')):
        if fields[target] != raw[source]: raise InvalidData()
    for name in ('known_subtotal_atoms', 'held_atoms', 'quota_held'):
        integer(item[name])
    for name in ('known_cost_atoms', 'estimated_cost_atoms', 'reported_cost_atoms', 'quota_known'):
        integer(item[name], True)
    for name in ('cost_complete', 'valid', 'cost_disagreement'):
        boolean(item[name])
    identifier(item['price_revision'])
    enum(item['source'], ('PROVIDER_REPORTED', 'LOCALLY_ESTIMATED', 'UNAVAILABLE'))
    enum(item['coverage'], ('COMPLETE', 'PARTIAL', 'UNAVAILABLE'))
    incoming, cached, outgoing = (fields[name] for name in ('input_tokens', 'cache_read_tokens', 'output_tokens'))
    if item['valid']:
        try:
            if incoming is not None and outgoing is not None and fields['total_tokens'] is not None and add(quantity(incoming), quantity(outgoing)) != fields['total_tokens']:
                raise InvalidData()
            for part, whole in ((cached, incoming), (fields['reasoning_tokens'], outgoing)):
                if part is not None and whole is not None and quantity(part) > quantity(whole): raise InvalidData()
        except InvalidAmount:
            raise InvalidData() from None
    expected_quantities = {'input': quantity(incoming)-quantity(cached) if type(incoming) is int and type(cached) is int and incoming >= cached else None,
        'cached_input': cached, 'output': outgoing, 'subscription_request': None}
    if not_sent:
        if any(value is not None for value in raw.values()): raise InvalidData()
        expected_quantities = dict.fromkeys(expected_quantities, 0)
    names = ('input', 'cached_input', 'output') if item['billing_mode'] == 'TOKEN_METERED' else ('subscription_request',)
    parts = item['items']
    if type(parts) is not tuple or len(parts) != len(names): raise InvalidData()
    costs: list[int] = []
    complete_items = True
    for name, part in zip(names, parts, strict=True):
        part = exact(part, 'item quantity price_numerator price_denominator cost_atoms')
        if part['item'] != name: raise InvalidData()
        for field in ('quantity', 'price_numerator', 'price_denominator', 'cost_atoms'):
            integer(part[field], True)
        if part['quantity'] != expected_quantities[name]: raise InvalidData()
        if name != 'subscription_request':
            integer(part['price_numerator'])
            if part['price_denominator'] != 1000000: raise InvalidData()
            if part['cost_atoms'] is not None:
                try:
                    if part['cost_atoms'] != rounded_cost(quantity(part['quantity']), quantity(part['price_numerator'])):
                        raise InvalidData()
                except InvalidAmount:
                    raise InvalidData() from None
        elif part['price_numerator'] is not None or part['price_denominator'] is not None:
            raise InvalidData()
        complete_items &= part['cost_atoms'] is not None
        if part['cost_atoms'] is not None: costs.append(cast(int, part['cost_atoms']))
    try:
        estimated = add(*costs) if complete_items else None
    except InvalidAmount:
        raise InvalidData() from None
    if item['estimated_cost_atoms'] != estimated: raise InvalidData()
    if item['source'] != ('LOCALLY_ESTIMATED' if estimated is not None else 'UNAVAILABLE') or item['cost_disagreement'] is not False:
        raise InvalidData()
    if item['reported_cost_atoms'] is not None: raise InvalidData()
    if item['cost_complete']:
        if (item['known_cost_atoms'] != estimated or item['known_subtotal_atoms'] != estimated
                or estimated is None or item['held_atoms'] != 0 or not item['valid'] or item['coverage'] != 'COMPLETE'):
            raise InvalidData()
        if not not_sent and (item['billing_mode'] != 'TOKEN_METERED' or any(value is None for value in (incoming, cached, outgoing))
                or any(raw[name] not in (None, 0) for name in ('provisioned_input_tokens', 'provisioned_output_tokens'))):
            raise InvalidData()
    elif item['known_cost_atoms'] is not None or item['known_subtotal_atoms'] != 0:
        raise InvalidData()
    if item['billing_mode'] == 'TOKEN_METERED' and (item['quota_known'] != 0 or item['quota_held'] != 0): raise InvalidData()
    return item


def _cause(value: object) -> None:
    if value is not None:
        item = exact(value, 'code field reason')
        allowed = (('UNSUPPORTED_CAPABILITY', 'capability', 'PROTOCOL_UNSUPPORTED'),
                   ('ADAPTER_FAILED', 'adapter', 'OUTPUT_LIMIT'),
                   ('CONFIGURATION_REJECTED', 'configuration', 'MODEL_BINDING_MISMATCH'),
                   ('PAUSED_BUDGET', 'budget', 'BILLING_EVIDENCE_MISSING'))
        if (item['code'], item['field'], item['reason']) in allowed: return
    cause(value)


def validate(table: str, value: Record) -> None:
    """Validate the selected complete row; unknown tables and fields fail closed."""
    if table not in SCHEMAS: raise InvalidData()
    schema = 'object_id revision ' + SCHEMAS[table]
    if table == 'handoffs' and 'payload' in value: schema += ' payload'
    exact(freeze(value, 65536 if table == 'handoffs' else 8192, owned=True), schema)
    identifier(value['object_id']); integer(value['revision'])
    for name in ('created_at', 'updated_at'):
        if name in value: timestamp(value[name])
    if table=='cost_items' and value['format_version']==3:
        from .usage_only import validate_item
        validate_item(value)
        return
    if 'format_version' in value: integer(value['format_version'], minimum=2, maximum=2)
    if table == 'requests':
        for name in ('caller_module', 'caller_scope', 'operation_key', 'result_owner', 'profile_id', 'config_snapshot_id', 'profile_revision', 'price_revision'):
            identifier(value[name])
        for name in ('extension_id', 'account_id', 'handoff_id'): identifier(value[name], True)
        enum(value['capability'], ('GENERATION',)); enum(value['task_role'], ('LEARNING', 'PERSONA'))
        enum(value['source'], ('REMOTE_PROVIDER',)); enum(value['configuration_origin'], ('PERSISTED_CONFIGURATION',))
        integer(value['fingerprint_version'], minimum=2, maximum=2); checksum(value['fingerprint'])
        integer(value['attempt_count'], maximum=1); boolean(value['ever_unknown']); _cause(value['first_error'])
        enum(value['phase'], ('OPEN', 'TERMINAL', 'REMOTE_RESULT_UNKNOWN'))
        if value['phase'] == 'TERMINAL': enum(value['outcome'], TERMINALS)
        elif value['outcome'] is not None: raise InvalidData()
        attribution = exact(value['attribution'], 'run_id entry_ids parent_request_id trace_id batch_id dream_run_id prompt_revision')
        identifier(attribution['run_id']); identifiers(attribution['entry_ids'])
        for name in ('parent_request_id', 'trace_id', 'batch_id', 'dream_run_id', 'prompt_revision'): identifier(attribution[name], True)
        evidence = exact(value['execution_evidence'], 'profile account request_timeout_ms retry_delay_ms request_max_bytes result_max_bytes')
        for name in ('request_timeout_ms', 'retry_delay_ms', 'request_max_bytes', 'result_max_bytes'): integer(evidence[name])
        if evidence['profile'] is not None:
            p, a = profile(evidence['profile']), account(evidence['account'])
            if (p['account_id'] != a['account_id'] or p['account_id'] != value['account_id'] or p['profile_id'] != value['profile_id']
                    or as_record(a['price'])['revision_ref'] != value['price_revision']): raise InvalidData()
            if a['attempt_limit']!=(14 if p['model_id']=='deepseek-flash' else 16):raise InvalidData()
            if p['model_id']=='deepseek-flash' and (a['billing_mode']!='TOKEN_METERED' or a['currency']!='CNY'):raise InvalidData()
        elif evidence['account'] is not None or value['account_id'] is not None: raise InvalidData()
    elif table == 'attempts':
        for name in ('request_id', 'account_id', 'profile_id', 'execution_owner_id'): identifier(value[name])
        integer(value['ordinal'], minimum=1, maximum=1); integer(value['evidence_revision']); integer(value['adapter_duration_ms'], True)
        enum(value['state'], ('PREPARED', 'COMPLETED', 'NOT_SENT', 'REMOTE_RESULT_UNKNOWN')); enum(value['logical_outcome'], TERMINALS, True)
        enum(value['capability'], ('GENERATION',)); enum(value['wire_protocol'], ('OPENAI_CHAT_COMPLETIONS','MINIMAX_CHAT_JSON_V1','DEEPSEEK_CHAT_JSON_V1'))
        boolean(value['confirmed_started'], True); boolean(value['ever_unknown']); _cause(value['first_error'])
        identifier(value['handoff_id'], True); checksum(value['result_fingerprint'], True); validate_usage(value['usage'], not_sent=value['state'] == 'NOT_SENT')
        expected_usage={'OPENAI_CHAT_COMPLETIONS':2,'MINIMAX_CHAT_JSON_V1':3,'DEEPSEEK_CHAT_JSON_V1':4}[str(value['wire_protocol'])]
        if as_record(value['usage'])['format_version']!=expected_usage:raise InvalidData()
        _cause(value['terminal_error'])
        if value['state'] in ('PREPARED','REMOTE_RESULT_UNKNOWN'):
            if value['terminal_error'] is not None or value['logical_outcome'] is not None:raise InvalidData()
        elif value['logical_outcome'] is None:
            raise InvalidData()
        elif value['logical_outcome']=='SUCCEEDED':
            if value['terminal_error'] is not None or value['state']!='COMPLETED':raise InvalidData()
        elif value['terminal_error'] is None or value['first_error'] is None:
            raise InvalidData()

    elif table == 'budget_windows':
        identifier(value['account_id']); identifier(value['window_id']); policy = account(value['policy'])
        if (policy['account_id'], policy['window_id']) != (value['account_id'], value['window_id']): raise InvalidData()
        for name in ('attempt_count', 'known_subtotal_atoms', 'held_atoms', 'quota_reserved', 'quota_known', 'quota_held'): integer(value[name])
        enum(value['risk_state'], ('CLEAR', 'RESERVATION_OVERRUN'))
    elif table == 'reservations':
        for name in ('attempt_id', 'account_id', 'budget_id'): identifier(value[name])
        for name in ('reserved_atoms', 'known_subtotal_atoms', 'held_atoms', 'quota_reserved', 'quota_known', 'quota_held'): integer(value[name])
        integer(value['known_cost_atoms'], True); boolean(value['cost_complete'])
        if value['cost_complete']:
            if value['held_atoms'] != 0 or value['known_cost_atoms'] != value['known_subtotal_atoms']: raise InvalidData()
        elif value['known_cost_atoms'] is not None or value['held_atoms'] != value['reserved_atoms'] or value['known_subtotal_atoms'] != 0:
            raise InvalidData()
    elif table == 'cost_items':
        identifier(value['attempt_id']); enum(value['item'], ('input', 'cached_input', 'output', 'subscription_request', 'reported'))
        integer(value['evidence_revision']); integer(value['known_subtotal_atoms']); boolean(value['cost_complete'])
        for name in ('quantity', 'price_numerator', 'price_denominator', 'cost_atoms'): integer(value[name], True)
        enum(value['source'], ('PROVIDER_REPORTED', 'LOCALLY_ESTIMATED', 'UNAVAILABLE'))
        enum(value['unit'], ('TOKEN', 'SUBSCRIPTION_REQUEST', 'CURRENCY_ATOM'))
        if value['known_subtotal_atoms'] != (value['cost_atoms'] or 0) or value['cost_complete'] != (value['cost_atoms'] is not None): raise InvalidData()
        expected_unit = 'CURRENCY_ATOM' if value['item'] == 'reported' else 'SUBSCRIPTION_REQUEST' if value['item'] == 'subscription_request' else 'TOKEN'
        expected_source = 'LOCALLY_ESTIMATED' if value['cost_atoms'] is not None else 'UNAVAILABLE'
        if value['unit'] != expected_unit or value['source'] != expected_source: raise InvalidData()
        if value['item'] in ('reported', 'subscription_request'):
            if value['price_numerator'] is not None or value['price_denominator'] is not None: raise InvalidData()
            if value['item'] == 'reported' and (value['quantity'] is not None or value['cost_atoms'] is not None): raise InvalidData()
        elif value['price_denominator'] != 1000000 or type(value['price_numerator']) is not int:
            raise InvalidData()
    else:
        for name in ('request_id', 'owner_id', 'artifact_id'): identifier(value[name])
        checksum(value['checksum']); enum(value['source'], ('REMOTE_PROVIDER',))
        if 'payload' in value and type(value['payload']) is not str: raise InvalidData()


def terminal_reason(request: Record, attempts: tuple[Record,...]) -> str | None:
    """Derive the final reason from current complete rows, never a prior timeout.

    The zero-attempt path has only its actual registered request terminal. Every
    sent/unsent attempt must agree with the request before issuing native proof.
    """
    if request['phase']!='TERMINAL' or len(attempts)!=request['attempt_count'] or len(attempts)>1:raise InvalidData()
    final=request['first_error']
    if attempts:
        attempt=attempts[0];validate('attempts',attempt)
        if (attempt['request_id']!=request['object_id'] or attempt['ordinal']!=1
                or attempt['state'] not in ('COMPLETED','NOT_SENT') or attempt['logical_outcome']!=request['outcome']
                or any(attempt[name]!=request[name] for name in ('account_id','profile_id','capability','handoff_id'))):raise InvalidData()
        final=attempt['terminal_error']
    if request['outcome']=='SUCCEEDED':
        if not attempts or final is not None:raise InvalidData()
        return None
    if final is None:raise InvalidData()
    return cast(str,as_record(final)['reason'])
