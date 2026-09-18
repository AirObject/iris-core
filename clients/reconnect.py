"""Finite jittered reconnect for the example host; no business-write replay.

Run the explicit HTTP operations in http_client.py with retained input files.
Then run iris_client.py with --takeover after a process restart. A connection
failure restores only authentication, this route's subscription, and current
status/goals. Receipts must still be confirmed with the original HTTP inputs.
"""
from __future__ import annotations
import json
import random
import time
from typing import Any


def listen(client: Any, route: str, entry: str, *, acknowledge: bool, takeover: bool,
           reconnect_attempts: int) -> None:
    """Bound retries across the whole invocation, including subscription refusal."""
    if not 0 <= reconnect_attempts <= 8: raise ValueError('Reconnect attempts must be between zero and eight.')
    for attempt in range(reconnect_attempts + 1):
        if attempt:
            delay=min(30.0,2.0**(attempt-1))*random.uniform(.8,1.2)
            delay=min(30.0,delay)
            print(json.dumps({'reconnect_attempt':attempt,'delay_seconds':round(delay,3),'business_replay':False}),flush=True)
            time.sleep(delay)
        ws=None
        try:
            capabilities=client.request('/api/host/capabilities',{})
            if capabilities.get('outcome')!='OBSERVED':
                print(json.dumps(capabilities,ensure_ascii=False),flush=True)
                return
            allowed=capabilities['data']['event_types']
            events=[event for event in ('goal.upcoming','goal.due','core.mode_changed','connection.probe') if event in allowed]
            ws=client.websocket()
            print(json.dumps(ws.receive(),ensure_ascii=False),flush=True)
            ws.send({'version':1,'type':'subscribe','request_id':'subscribe','route_ids':[route],
                'event_types':events,'takeover':takeover})
            while True:
                message=ws.receive()
                print(json.dumps(message,ensure_ascii=False),flush=True)
                if message.get('type')=='subscription_changed' and message.get('route_id')==route:
                    return
                if message.get('type')=='subscription_result' and message.get('state')=='SUBSCRIBED':
                    print(json.dumps(client.request('/api/host/notifications/status',{'route_ids':[route]}),ensure_ascii=False),flush=True)
                    print(json.dumps(client.request('/api/host/notifications/results',{'route_id':route,'after':''}),ensure_ascii=False),flush=True)
                    if 'goal_read' in capabilities['data']['operations']:
                        print(json.dumps(client.request('/api/host/goals',{'entry_id':entry,'input':{}}),ensure_ascii=False),flush=True)
                if acknowledge and message.get('event') in ('goal.upcoming','goal.due','connection.probe'):
                    ws.send({'version':1,'type':'ack','route_id':message['route_id'],'delivery_id':message['delivery_id'],'status':'RECEIVED'})
                if message.get('type')=='error':
                    if message.get('reconnect') is True: break
                    return
        except (OSError,ConnectionError):
            if attempt==reconnect_attempts: raise
        finally:
            if ws is not None: ws.close()
    raise ConnectionError('Finite subscription recovery budget exhausted.')
