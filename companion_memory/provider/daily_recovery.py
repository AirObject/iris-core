"""Bounded reverse checks for the single mixed Provider ledger at startup.

Reservations remain conservative after an observation-only commit. Terminal
settlement, cost items, handoffs and each account total must agree in both
directions. The checker neither initializes budgets nor activates a request.
"""
from typing import cast
from companion_memory.persistence import Found
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.semantic_records import identity
from companion_memory.configuration import PresentValue
from .values import as_record
from .daily_stored_schema import OWNERS,DREAM_OWNERS


async def recover_confirmed_unsent(provider):
    """Settle already-persisted no-send evidence without inventing remote doubt."""
    from .daily_execution import row,settlement
    from .ledger import Mutation
    after=''
    while page:=await provider.ledger.read('requests_page',{'after':after,'limit':8}):
        for request in page:
            provider.checkpoint();after=request['object_id']
            if request['capability']=='EMBEDDING' or request['phase']!='OPEN':continue
            attempts=await provider.ledger.read('attempts_for_request',{'request_id':request['object_id']})
            if len(attempts)!=1:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
            attempt=attempts[0]
            if attempt['confirmed_started'] is not False or attempt['evidence_revision']!=1:continue
            evidence=await provider.ledger.operations['evidence'].read_receipt(identity('daily-evidence',request['object_id']))
            reservation=await provider.ledger.get('reservations',identity('daily-reservation',attempt['object_id']))
            if type(evidence) is not Found or reservation is None:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
            budget=await provider.ledger.get('budget_windows',reservation['budget_id'])
            if budget is None:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
            metering=as_record(attempt['usage']);error=attempt['first_error']
            if error is None or not metering['cost_complete'] or metering['known_cost_atoms']!=0 or metering['held_atoms']!=0:
                raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
            outcome='MODE_BLOCKED' if as_record(error)['reason']=='OPERATION_NOT_GRANTED' else 'FAILED'
            now=provider._now()
            ended=row(dict(attempt)|{'revision':attempt['revision']+1,'updated_at':now,'state':'NOT_SENT','logical_outcome':outcome,'terminal_error':error})
            terminal=row(dict(request)|{'revision':request['revision']+1,'updated_at':now,'phase':'TERMINAL','outcome':outcome,'first_error':error})
            changes=(Mutation('requests',request,terminal),Mutation('attempts',attempt,ended),*settlement(budget,reservation,metering,attempt['object_id']))
            await provider._commit('terminate',identity('daily-terminate',request['object_id']),changes,request['object_id'],attempt['object_id'],'OPEN','TERMINAL',True)


async def verify_mixed_ledger(provider):
    owners=DREAM_OWNERS if provider.ledger.assembly.dream_format else OWNERS
    values=next(e.state.value for e in provider.configuration.candidate.foundation.list_entries()
        if e.definition.key=='provider.accounts' and type(e.state) is PresentValue)
    accounts={cast(str,a['account_id']):a for a in map(as_record,cast(tuple,values))}
    profiles={p['profile_id']:p for p in provider.profiles}
    totals={key:[0,0,0] for key in accounts};requests=0

    def require(condition):
        if not condition:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')

    async def pages(table):
        after=''
        while page:=await provider.ledger.read(table+'_page',{'after':after,'limit':8}):
            for value in page:
                provider.checkpoint();yield value;after=cast(str,value['object_id'])

    async def get(table,key):
        provider.checkpoint();value=await provider.ledger.get(table,key)
        require(value is not None)
        return value

    async def receipt(port,key):
        provider.checkpoint();found=await port.read_receipt(key)
        require(type(found) is Found)
        return found.value

    async for request in pages('requests'):
        requests+=1;role=request['task_role'];profile=profiles.get(request['profile_id']);account=accounts.get(request['account_id'])
        if provider.managed_versions is not None:
            # Ledger decoding has already verified this immutable evidence
            # against its configuration-owner-issued original version.
            profile=as_record(as_record(request['execution_evidence'])['profile'])
        if profile is None or account is None or role not in owners:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        evidence=as_record(request['execution_evidence']);embedding=request['capability']=='EMBEDDING'
        prefix='embedding' if embedding else 'daily'
        require(request['caller_scope']==provider.instance and request['caller_module']==owners[role] and request['result_owner']==owners[role]
            and request['config_snapshot_id']==provider.configuration.snapshot_id and request['format_version']==provider.ledger.assembly.version
            and evidence['profile']==profile and evidence['account']==account and profile['account_id']==account['account_id']
            and profile['material_role']==role and profile['capability']==request['capability']
            and request['object_id']==identity(prefix+'-request',provider.instance,request['operation_key']))
        attempts=await provider.ledger.read('attempts_for_request',{'request_id':request['object_id']})
        require(len(attempts)==request['attempt_count']==1)
        attempt=attempts[0]
        # A plausible collection of rows cannot replace their original atomic
        # operations and required audit records, including earlier registration.
        registration_port=provider.ledger.operations['register'] if embedding else provider.chat_operations['register_daily_request']
        await receipt(registration_port,identity(prefix+'-register',request['object_id']))
        if attempt['evidence_revision'] and (not embedding or request['outcome']=='SUCCEEDED' or request['phase']!='TERMINAL'):
            await receipt(provider.ledger.operations['evidence'],identity(prefix+'-evidence',request['object_id']))
        if request['phase']=='TERMINAL':
            if request['outcome']=='SUCCEEDED':
                completion_port=provider.operations['store_embedding_handoff'] if embedding else provider.chat_operations['store_daily_handoff']
                await receipt(completion_port,identity(prefix+'-complete',request['object_id']))
            else:await receipt(provider.ledger.operations['terminate'],identity(prefix+'-terminate',request['object_id']))
        elif request['phase']=='REMOTE_RESULT_UNKNOWN':
            await receipt(provider.ledger.operations['recover'],identity('embedding-unknown',request['object_id']))
        require(attempt['object_id']==identity(prefix+'-attempt',request['object_id'],1) and attempt['ordinal']==1
            and all(attempt[k]==request[k] for k in ('account_id','profile_id','capability')) and attempt['wire_protocol']==profile['wire_protocol'])
        reservation=await get('reservations',identity(prefix+'-reservation',attempt['object_id']))
        require(reservation['attempt_id']==attempt['object_id'] and reservation['account_id']==request['account_id'])
        metering=as_record(attempt['usage'])
        if request['phase']=='TERMINAL':
            require(attempt['state'] in ('COMPLETED','NOT_SENT') and attempt['logical_outcome']==request['outcome']
                and attempt['handoff_id']==request['handoff_id'] and all(reservation[k]==metering[k] for k in ('known_subtotal_atoms','held_atoms','cost_complete','known_cost_atoms')))
            for raw in cast(tuple,metering['items']):
                part=as_record(raw);key=identity('embedding-cost',attempt['object_id']) if embedding else identity('daily-cost',attempt['object_id'],cast(str,part['item']))
                cost=await get('cost_items',key)
                require(cost['attempt_id']==attempt['object_id'] and cost['item']==part['item'] and cost['evidence_revision']==attempt['evidence_revision']
                    and all(cost[k]==part[k] for k in ('quantity','price_numerator','price_denominator','cost_atoms')))
            if request['outcome']=='SUCCEEDED':
                handoff=await get('handoffs',request['handoff_id'])
                require(handoff['request_id']==request['object_id'] and handoff['owner_id']==request['result_owner'] and handoff['checksum']==attempt['result_fingerprint'])
            else:require(request['handoff_id'] is None)
        elif request['phase']=='OPEN':require(attempt['state']=='PREPARED')
        else:require(request['phase']=='REMOTE_RESULT_UNKNOWN' and attempt['state']=='REMOTE_RESULT_UNKNOWN')

    async for attempt in pages('attempts'):
        request=await get('requests',attempt['request_id'])
        prefix='embedding' if request['capability']=='EMBEDDING' else 'daily'
        require(attempt['object_id']==identity(prefix+'-attempt',request['object_id'],1))
    async for reservation in pages('reservations'):
        attempt=await get('attempts',reservation['attempt_id']);request=await get('requests',attempt['request_id'])
        prefix='embedding' if request['capability']=='EMBEDDING' else 'daily';account=accounts.get(reservation['account_id'])
        if account is None:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        require(reservation['account_id']==request['account_id']
            and reservation['object_id']==identity(prefix+'-reservation',attempt['object_id'])
            and reservation['budget_id']==identity(prefix+'-budget',cast(str,account['account_id']),cast(str,account['window_id'])))
        budget=await get('budget_windows',reservation['budget_id']);require(budget['account_id']==account['account_id'])
        total=totals[cast(str,account['account_id'])];total[0]+=1;total[1]+=reservation['known_subtotal_atoms'];total[2]+=reservation['held_atoms']
    async for budget in pages('budget_windows'):
        account=accounts.get(budget['account_id'])
        if account is None:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        prefix='embedding' if account['account_id']==provider.account['account_id'] else 'daily'
        require(budget['object_id']==identity(prefix+'-budget',cast(str,account['account_id']),cast(str,account['window_id']))
            and budget['window_id']==account['window_id'] and budget['policy']==account
            and totals[cast(str,account['account_id'])]==[budget[k] for k in ('attempt_count','known_subtotal_atoms','held_atoms')])
    async for cost in pages('cost_items'):
        attempt=await get('attempts',cost['attempt_id']);request=await get('requests',attempt['request_id'])
        require(request['phase']=='TERMINAL' and attempt['state'] in ('COMPLETED','NOT_SENT'))
        parts=[as_record(p) for p in cast(tuple,as_record(attempt['usage'])['items']) if as_record(p)['item']==cost['item']]
        require(len(parts)==1)
        key=identity('embedding-cost',attempt['object_id']) if request['capability']=='EMBEDDING' else identity('daily-cost',attempt['object_id'],cost['item'])
        require(cost['object_id']==key and all(cost[k]==parts[0][k] for k in ('quantity','price_numerator','price_denominator','cost_atoms')))
    async for handoff in pages('handoffs'):
        request=await get('requests',handoff['request_id'])
        require(request['phase']=='TERMINAL' and request['outcome']=='SUCCEEDED' and request['handoff_id']==handoff['object_id'] and request['result_owner']==handoff['owner_id'])
        cleanup=as_record(handoff['embedding_cleanup']);payload=as_record(handoff['embedding_payload'])
        start=0 if cleanup['retired_through'] is None else cast(int,cleanup['retired_through'])+1
        count=cast(int,payload['leaf_count'])
        require(0<=start<=count and (cleanup['state']=='RETIRED')==(start==count))
        leaves=[]
        for ordinal in range(count):
            provider.checkpoint()
            leaf=await provider.rows.read('embedding_handoff_leaf',identity('embedding-handoff-leaf',handoff['object_id'],ordinal))
            require((leaf is None)==(ordinal<start))
            if leaf is not None:leaves.append(leaf)
        if start==0:
            arguments={'handoff_id':handoff['object_id'],'request_id':request['object_id'],
                'attempt_id':identity(('embedding' if request['capability']=='EMBEDDING' else 'daily')+'-attempt',request['object_id'],1)}
            if request['capability']=='EMBEDDING':
                from .embedding_material import restore_handoff
                profile=profiles[request['profile_id']]
                restore_handoff(payload,leaves,space_id=profile['space_id'],model_id=profile['model_id'],**arguments)
            else:
                from .daily_handoff import restore
                restore(payload,leaves,provider.bindings[request['task_role']],**arguments)
        if request['capability']!='EMBEDDING' and cleanup['state']!='HELD':
            await receipt(provider.chat_operations['confirm_daily_handoff'],identity('daily-received',request['object_id']))
            if start:
                await receipt(provider.chat_operations['retire_daily_handoff'],identity('daily-retire',request['object_id'],handoff['revision']-1))
    after=''
    while page:=await provider.rows.page('embedding_handoff_leaf',after,8):
        for leaf in page:
            provider.checkpoint();after=leaf['row_id']
            handoff=await get('handoffs',leaf['handoff_id']);request=await get('requests',leaf['request_id'])
            cleanup=as_record(handoff['embedding_cleanup']);payload=as_record(handoff['embedding_payload'])
            require(handoff['request_id']==request['object_id'] and request['handoff_id']==handoff['object_id']
                and leaf['attempt_id']==identity(('embedding' if request['capability']=='EMBEDDING' else 'daily')+'-attempt',request['object_id'],1)
                and leaf['ordinal']<payload['leaf_count'] and cleanup['state']!='RETIRED'
                and (cleanup['retired_through'] is None or leaf['ordinal']>cleanup['retired_through']))
    return requests
