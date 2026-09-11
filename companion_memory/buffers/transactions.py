"""Buffer-owned FIFO membership, explicit references and one atomic terminal rotation.

History and live positions are separate from frozen members. Reference removal
is owner-specific, so terminal cleanup cannot erase another source owner's data.
"""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import UnitOfWork,Value
from companion_memory.runtime.records import OwnedRows,DomainFailure,data,stable_id


class BufferTransactions:
    def __init__(self,rows:OwnedRows):
        self.rows=rows

    def register(self,uow:UnitOfWork,entry_id:str,platform_id:str) -> None:
        if self.rows.get('entry_state',entry_id,uow) is None:
            self.rows.insert('entry_state',uow,entry_id,entry_id,0,'NONE',{'next_sequence':0,'history':[],'transfer_cursor':0,'transferred_count':0,'platform_id':platform_id,'earliest_received_at_us':None,'latest_received_at_us':None})

    def allocate(self,uow:UnitOfWork,entry_id:str) -> tuple[int,dict[str,object]]:
        state=self.rows.get('entry_state',entry_id,uow)
        if state is None:raise DomainFailure('STORAGE_FAILED','entry','INTEGRITY_FAILURE')
        previous=cast(int,data(state)['next_sequence'])
        if previous>=2**63-1:raise DomainFailure('INVALID_INPUT','entry','LIMIT_EXCEEDED')
        changed=self.rows.update('entry_state',uow,state,cast(str,state['state']),{**data(state),'next_sequence':previous+1})
        return previous+1,changed

    def reference(self,uow:UnitOfWork,entry_id:str,message_id:str,sequence:int,owner:str) -> None:
        key=stable_id('reference',owner,message_id)
        if self.rows.get('references',key,uow) is None:
            self.rows.insert('references',uow,key,entry_id,sequence,message_id,{'owner':owner,'message_id':message_id})

    def unreference(self,uow:UnitOfWork,message_id:str,owner:str) -> None:
        row=self.rows.get('references',stable_id('reference',owner,message_id),uow)
        if row is not None:self.rows.delete('references',uow,row)

    def referenced(self,uow:UnitOfWork,entry_id:str,message_id:str) -> bool:
        rows=self.rows.participate('references_count',uow,{'entry_id':entry_id,'state':message_id})
        return cast(int,rows[0]['count'])>0

    def append(self,uow:UnitOfWork,entry_id:str,message_id:str,sequence:int,placement:str,time_us:int) -> None:
        self.rows.insert('positions',uow,message_id,entry_id,sequence,'NORMAL' if placement=='NORMAL_PENDING' else 'STAGED',{})
        self.reference(uow,entry_id,message_id,sequence,'pending')
        state=self.rows.get('entry_state',entry_id,uow)
        if state is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        values=data(state)
        self.rows.update('entry_state',uow,state,'PENDING' if placement!='NORMAL_PENDING' else cast(str,state['state']),
            {**values,'earliest_received_at_us':values['earliest_received_at_us'] if values['earliest_received_at_us'] is not None else time_us,'latest_received_at_us':time_us})

    def page(self,uow:UnitOfWork,table:str,entry_id:str,state:str,limit:int,after:int=0) -> tuple[MappingProxyType[str,Value],...]:
        return self.rows.participate(table+'_entry',uow,{'entry_id':entry_id,'state':state,'after_sequence':after,'after_id':'','limit':limit})

    def freeze(self,uow:UnitOfWork,entry_id:str,batch_id:str,run_id:str,configuration_id:str,target:int,recent:int,history:int,material_digest:str,member_values:tuple[dict[str,object],...],created_at_us:int,target_times:tuple[int,int]) -> dict[str,object]:
        existing=self.rows.get('batches',batch_id,uow)
        if existing is not None:raise DomainFailure('PRECONDITION_FAILED','batch','TARGET_CHANGED')
        normal=self.page(uow,'positions',entry_id,'NORMAL',target+recent)
        if len(normal)<target+recent:raise DomainFailure('INVALID_STATE','trigger','NOT_READY')
        target_rows=normal[:target];recent_rows=normal[target:target+recent]
        state=self.rows.get('entry_state',entry_id,uow)
        if state is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        hids=cast(list[str],data(state)['history'])
        expected=tuple(hids)+tuple(cast(str,r['object_id']) for r in normal)
        if tuple(m['message_id'] for m in member_values)!=expected or tuple(m['role'] for m in member_values)!=('H',)*len(hids)+('T',)*target+('R',)*recent or len(hids)>history:
            raise DomainFailure('PRECONDITION_FAILED','batch','TARGET_CHANGED')
        batch=self.rows.insert('batches',uow,batch_id,entry_id,cast(int,target_rows[0]['sequence']),'FROZEN',{
            'run_id':run_id,'config_snapshot_id':configuration_id,'created_at_us':created_at_us,'material_protocol':'synthetic_window_base64:1','template_protocol':'synthetic_target_refs:1','participant_protocol':'synthetic_learning:1','target_count':len(target_rows),'history_count':len(hids),'recent_count':len(recent_rows),
            'range_start':target_rows[0]['sequence'],'range_end':target_rows[-1]['sequence'],'range_start_us':target_times[0],'range_end_us':target_times[1],'material_digest':material_digest,'candidate_id':None,'terminal':None,'result_count':None,
        })
        for member in member_values:
            mid=cast(str,member['message_id']);seq=cast(int,member['entry_seq'])
            self.rows.insert('members',uow,stable_id('member',batch_id,mid),entry_id,seq,batch_id,member)
            self.reference(uow,entry_id,mid,seq,batch_id)
        return batch

    def members(self,uow:UnitOfWork,entry_id:str,batch_id:str) -> tuple[dict[str,object],...]:
        refs=self.page(uow,'members',entry_id,batch_id,128)
        result=[]
        for ref in refs:
            row=self.rows.get('members',cast(str,ref['object_id']),uow)
            if row is None:raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
            result.append(data(row))
        return tuple(result)

    def terminate(self,uow:UnitOfWork,batch:dict[str,object],terminal:str,result_count:int,history_limit:int) -> tuple[tuple[str,...],int]:
        batch_id=cast(str,batch['object_id']);entry_id=cast(str,batch['entry_id'])
        members=self.members(uow,entry_id,batch_id)
        targets=tuple(m for m in members if m['role']=='T')
        state=self.rows.get('entry_state',entry_id,uow)
        if state is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        old_history=cast(list[str],data(state)['history'])
        new_history=tuple(cast(str,m['message_id']) for m in targets[-history_limit:]) if history_limit and terminal!='SENSITIVE_DROPPED' else ()
        by_id={cast(str,m['message_id']):m for m in members}
        for mid in new_history:self.reference(uow,entry_id,mid,cast(int,by_id[mid]['entry_seq']),'history:'+entry_id)
        for mid in old_history:
            if mid not in new_history:self.unreference(uow,mid,'history:'+entry_id)
        for m in targets:
            mid=cast(str,m['message_id'])
            position=self.rows.get('positions',mid,uow)
            if position is None or position['state']!='NORMAL':raise DomainFailure('PRECONDITION_FAILED','batch','TARGET_CHANGED')
            self.rows.delete('positions',uow,position)
            self.unreference(uow,mid,'pending')
        for m in members:self.unreference(uow,cast(str,m['message_id']),batch_id)
        self.rows.update('entry_state',uow,state,cast(str,state['state']),{**data(state),'history':new_history,'history_failed':terminal=='FAILED_DROPPED'})
        self.rows.update('batches',uow,batch,'TERMINAL',{**data(batch),'terminal':terminal,'result_count':result_count})
        return tuple(dict.fromkeys(old_history+[cast(str,m['message_id']) for m in members])),len(new_history)

    def transfer(self,uow:UnitOfWork,entry_id:str,expected_cursor:int,limit:int) -> tuple[int,int,int]:
        state=self.rows.get('entry_state',entry_id,uow)
        if state is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        previous=cast(int,data(state)['transfer_cursor'])
        if previous!=expected_cursor:raise DomainFailure('PRECONDITION_FAILED','entry','TRANSFER_CURSOR_CHANGED')
        page=self.page(uow,'positions',entry_id,'STAGED',limit)
        # Empty reads have no stable next sequence and must never reserve the
        # identity of a future real page at the same cursor.
        if not page:raise DomainFailure('PRECONDITION_FAILED','entry','TRANSFER_CURSOR_CHANGED')
        cursor=previous
        for meta in page:
            row=self.rows.get('positions',cast(str,meta['object_id']),uow)
            if row is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
            self.rows.update('positions',uow,row,'NORMAL',{})
            cursor=cast(int,row['sequence'])
        remaining=self.rows.participate('positions_count',uow,{'entry_id':entry_id,'state':'STAGED'})[0]['count']
        self.rows.update('entry_state',uow,state,'PENDING' if remaining else 'NONE',
            {**data(state),'transfer_cursor':cursor,'transferred_count':cast(int,data(state)['transferred_count'])+len(page)})
        return cursor,len(page),cast(int,remaining)
