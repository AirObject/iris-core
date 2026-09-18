"""Closed public observation and result carriers for the communication API."""
from __future__ import annotations
from .http_contracts import obj, nullable, enum, array, ID, TIME, BOOL, text


def extend(http: dict, native) -> None:
    from companion_memory.configuration.communication_schema import SCHEMAS as POLICIES
    from companion_memory.management.communication_records import ROUTE, PROBE
    from companion_memory.goals.records import DAILY_GOAL, SOURCE
    from companion_memory.state.records import ACTIVITY
    from companion_memory.goals.communication_records import GRACE
    schemas=http['components']['schemas'];paths=http['paths']
    def ref(name): return {'$ref':'#/components/schemas/'+name}
    error=ref('Error');receipt=ref('Receipt')
    schemas['RecoveryHandle']=obj({'identity':ref('OperationIdentity'),'command_version':TIME,
        'fingerprint_version':TIME,'fingerprint':text(64,64)})
    schemas['Committed']=obj({'receipt':receipt,'source':enum('NEW','EXISTING')})
    schemas['Failure']=obj({'error':nullable(error),'status':enum('REJECTED','NOT_COMMITTED','FAILED')},('status',))
    schemas['Unconfirmed']={'anyOf':[
        obj({'recovery_handle':ref('RecoveryHandle'),'error':error}),
        obj({'reference':ref('RecoveryHandle'),'error':error}),
        obj({'status':{'const':'UNCONFIRMED'},'operation_key':ID,'confirmation':ID,'error':error,'intent_digest':text(64,64)},('intent_digest',))]}
    duration={'duration_us':nullable(TIME),'duration_basis':enum('ACTUAL_START','FIRST_REPORT','UNKNOWN'),
        'clock':enum('OBSERVED','CLOCK_AHEAD','UNKNOWN')}
    ended=nullable(obj({'activity_id':ID,'revision':TIME,'ended_at':TIME,**duration}))
    schemas['StateView']={'anyOf':[
        obj({'activity':{'type':'null'},'last_ended_id':nullable(ID),'last_ended':ended,'observed_at':TIME,'pointer_revision':TIME}),
        obj({'activity':native(ACTIVITY),**duration,'stale':BOOL,'observed_at':TIME,'pointer_revision':TIME,
            'last_ended':ended,'field_durations':obj({key:nullable(obj(duration)) for key in ('scene','progress','emotion')})})]}
    goal=native(DAILY_GOAL)
    extra={'source_refs':array(native(SOURCE),8),'expired':BOOL,'dedup_unresolved':BOOL,
        'suggestion':nullable(enum('CONSIDER_ABANDON_OR_CHANGE_DEADLINE')),'semantic_review_available':BOOL,'observed_at':TIME}
    goal['properties'].update(extra);goal['required'].extend(extra)
    schemas['GoalView']=goal
    schemas['GoalPage']=obj({'items':array(ref('GoalView'),8),'has_more':BOOL,'omitted_count':nullable(TIME),
        'observed_at':TIME,'next_cursor':nullable(text(160))})
    route=native(ROUTE);extra={'open_goals':TIME,'online':BOOL,'connection_id':nullable(ID)}
    route['properties'].update(extra);route['required'].extend(extra)
    subscription=obj({'route_id':ID,'subscription_id':ID,'route_revision':TIME,'event_types':array(ID,4),'active':BOOL})
    connection=obj({'connection_id':ID,'token_id':ID,'host_id':nullable(ID),'test_route':nullable(ID),'opened_us':TIME,
        'configuration_id':ID,'subscriptions':array(subscription,8),'heartbeat_age_seconds':TIME,
        'queue_items':TIME,'queue_bytes':TIME,'closing':BOOL})
    delivery=obj({'delivery_id':ID,'route_id':ID,'connection_id':ID,'state':enum('WAITING_ACK','REGISTERED','ACKNOWLEDGED','UNKNOWN','NOT_SENT'),
        'remaining_ms':TIME,'cleanup_pending':BOOL})
    status={'observed_at_us':TIME,'mode':ID,'state':ID,'ws_enabled':BOOL,'ws_available':BOOL,'notifications_paused':BOOL,
        'configuration_id':nullable(ID),'policy':nullable(obj({key:native(value) for key,value in POLICIES.items()})),
        'routes':array(route,16),'connections':array(connection,16),'deliveries':array(delivery,8),
        'takeovers':array(obj({'route_id':ID,'old_connection_id':ID,'new_connection_id':ID,'observed_at_us':TIME}),64),
        'ack_received':nullable(TIME),'ack_confirmed':nullable(TIME)}
    schemas['NotificationStatus']=obj(status)
    from companion_memory.goals.records import PLAN_STATUS, ATTEMPT
    attempt_fields = {field.name: native(field.schema) for field in ATTEMPT.fields}
    schemas['DeliveryResultPage']=obj({'items':array(obj({'plan_id':ID,'route_id':ID,
        'kind':enum('UPCOMING','DUE'),'status':native(PLAN_STATUS),'updated_at':TIME,
        'attempts':array(obj({'delivery_id':ID,'state':attempt_fields['state'],'started_at':TIME,
            'finished_at':nullable(TIME),'reason':attempt_fields['reason']}),2)}),4),
        'after':nullable(ID),'observed_at_us':TIME})
    from companion_memory.configuration.managed_form import shape,plain
    from companion_memory.configuration.communication_schema import VALUES
    schemas['ConnectionOverview']=obj(status|{'origin':text(512),'websocket_path':{'const':'/api/host/ws'},
        'subprotocol':{'const':'iris.communication.v1'},'deployment_restart_required':{'const':True},
        'trusted_proxy_peers':text(512),'configuration_schema':{'const':{key:shape(value) for key,value in POLICIES.items()}},
        'defaults':{'const':plain(VALUES)}})
    schemas['ProbePage']=obj({'items':array(native(PROBE),4),'after':nullable(ID)})
    schemas['RoutePage']=obj({'items':array(native(ROUTE),4),'after':nullable(ID)})
    schemas['HostPage']=obj({'items':array(obj({'entry_id':ID,'host_id':ID,'platform_id':ID,'external_entry_id':text(512,1)}),16),'after':nullable(ID)})
    schemas['PlanPage']=obj({'items':array(obj({'plan_id':ID,'goal_id':ID,'kind':enum('UPCOMING','DUE'),
        'deadline':TIME,'due_at':TIME,'route_id':ID,'revision':TIME,'delivery_id':nullable(ID),'state':ID,
        'remaining_us':nullable(TIME),'effective_expires_us':nullable(TIME),'grace':nullable(native(GRACE))}),16),
        'after':nullable(ID),'observed_at_us':TIME})
    schemas['Ticket']=obj({'ticket':text(128,1),'expires_in_seconds':TIME,'route_id':ID,'event_types':array(enum('connection.probe'),1)})
    schemas['Disconnect']=obj({'state':enum('CLOSED','CLOSING'),'cleanup_pending':BOOL})
    schemas['ProbeStarted']=obj({'registration':{'anyOf':[ref('Committed'),ref('Failure'),ref('Unconfirmed')]},
        'probe_id':ID,'replayed':{'const':False},'model_requests':{'const':0}})
    responses={
        '/api/host/media/begin':ref('Committed'),'/api/host/media/finish':ref('Committed'),
        '/api/host/media/resolve':obj({'value':receipt}),'/api/host/media/inspect':ref('MediaInspection'),
        '/api/host/media/chunk':ref('MediaProgress'),'/api/host/capabilities':ref('Capabilities'),
        '/api/host/state':obj({'status':enum('FOUND'),'value':ref('StateView')},('status',)),'/api/host/goals':obj({'status':enum('FOUND'),'value':ref('GoalPage')},('status',)),
        '/api/host/notifications/status':ref('NotificationStatus'),
        '/api/host/notifications/results':ref('DeliveryResultPage'),
        '/api/connections/overview':ref('ConnectionOverview'),
        **{'/api/connections/'+name:ref(shape) for name,shape in (
            ('hosts/list','HostPage'),('routes/list','RoutePage'),('probes/list','ProbePage'),('plans/list','PlanPage'),
            ('tickets/create','Ticket'),('connections/disconnect','Disconnect'),('probes/run','ProbeStarted'))}}
    for path in paths:
        if path.endswith(('/register','/confirm','/create','/enable','/inject','/status','/deadline','/set','/update','/end','/accept','/accept/resolve','/operations/resolve','/usage','/usage/resolve')):
            responses.setdefault(path,ref('Committed'))
    import copy
    for path,success in responses.items():
        envelope=copy.deepcopy(schemas['Envelope'])
        envelope['properties']['data']={'anyOf':[success,ref('Failure'),ref('Unconfirmed'),obj({})]}
        if path in ('/api/host/state','/api/host/goals'): envelope['required']=['version','outcome']
        envelope['properties']['error']=error
        for response in paths[path]['post']['responses'].values():
            response['content']['application/json']['schema']=envelope
    # Outcomes describe persistence facts, independently of socket status.
    schemas['Envelope']['properties']['error']=error
    schemas['Envelope']['x-response-max-bytes']=1048576
