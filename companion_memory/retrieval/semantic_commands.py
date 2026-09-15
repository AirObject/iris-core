"""Closed retrieval payloads and the exact owners of semantic transactions.

Payload shape does not authorize work. Native bound owners must independently
verify mode, original receipts, object revisions and retained file evidence.
"""
from companion_memory.persistence.semantic_records import (ID,N,P,T,B,H,CONFIG,OBJECT,REQUEST,RECEIPT,INTENT,ERROR,
    record,enum,integer)
from companion_memory.persistence.schema import BoundedTextSchema

W={'work_id':ID,'expected_revision':P}
G={'generation_id':ID,'expected_revision':P}
PAYLOADS={
 'prepare':(
    record(kind=enum('EMBED'),work_id=ID,config=CONFIG,space_id=ID,purpose=enum('DOCUMENT','QUERY'),
        object_ref=(OBJECT,),change_seq=(N,),partition_id=ID,rendered_text=BoundedTextSchema(8192),original_request_key=ID),
    record(kind=enum('DELETE_LOCAL'),work_id=ID,config=CONFIG,space_id=ID,object_ref=OBJECT,change_seq=P,deletion_ref=RECEIPT)),
 'bind':(record(**W,intent=INTENT,deadline_at=T),),
 'record_result':(record(**W,request_ref=REQUEST,provider_completion=RECEIPT),),
 'apply':(record(kind=enum('EMBED'),**W,object_ref=OBJECT,latest_seq=P,artifact_id=ID),
          record(kind=enum('DELETE_LOCAL'),**W,object_ref=OBJECT,latest_seq=P,deletion_ref=RECEIPT)),
 'supersede':(record(**W,current_object_revision=P,current_change_seq=P),),
 'fail':(record(kind=enum('EMBED'),**W,error=ERROR,request_ref=(REQUEST,),terminal_receipt=(RECEIPT,)),
         record(kind=enum('DELETE_LOCAL'),**W,error=ERROR)),
 'pause':(record(space_id=ID,expected_revision=P,reason=enum('USER','BUDGET','MODE','UNKNOWN','RESOURCE','INTEGRITY')),),
 'begin_generation':(record(generation_id=ID,space_id=ID,captured_seq=N,expected_control_revision=P),),
 'append_page':(record(**G,page_no=integer(0,511),after_object_id=(ID,)),),
 'confirm_page':(record(**G,page_no=integer(0,511),page_digest=H),),
 'seal_generation':(record(**G,file_digest=H,file_bytes=N),),
 'publish_generation':(record(**G,expected_control_revision=P,file_digest=H,file_bytes=N),),
 'retire_page':(record(**G,after_row_id=(ID,),expected_control_revision=P),),
 'cache_bind':(record(cache_key=H,work_id=ID,expected_work_revision=P,expires_at=T),),
 'cache_expire':(record(cache_key=H,expected_revision=P,observed_at=T),),
 'gc_page':(record(space_id=ID,expected_control_revision=P,after_cache_key=(H,)),),
 'resume':(record(space_id=ID,expected_revision=P,authorization_digest=H),),
 'generation_fail':(record(**G,error=ERROR),),
 'record_cleanup':(record(kind=enum('EMBED'),**W,request_ref=(REQUEST,),provider_receipt=(RECEIPT,),cleanup_pending=B),
                   record(kind=enum('DELETE_LOCAL'),**W,completion_receipt=RECEIPT,cleanup_pending=B)),
}
WRITERS={kind:('retrieval','memory') if kind in ('apply','publish_generation') else ('retrieval',) for kind in PAYLOADS}
READERS={kind:('memory',) if kind in ('prepare','supersede','begin_generation','append_page') else
    ('provider',) if kind in ('record_result','fail','record_cleanup') else () for kind in PAYLOADS}
