"""Real temporary SQLite constraints and complete semantic record encodings.

Direct inserts here prove SQL constraints only. They do not represent the
public fixed-material, Provider, artifact reception or generation workflow.
"""
from dataclasses import replace
import json
import sqlite3
import tempfile
import unittest
from companion_memory.persistence._codec import schema_value,table_descriptor
from companion_memory.persistence.schema import encode_value,InvalidValue
from companion_memory.persistence.semantic_records import make_leaf,identity
from companion_memory.persistence.content_codec import encode_content
from companion_memory.memory.semantic_repository import semantic_memory_catalog
from companion_memory.retrieval.semantic_repository import semantic_retrieval_catalog
from companion_memory.provider.embedding_repository import embedding_handoff_catalog
from companion_memory.cognition.fixed_memory_repository import fixed_memory_catalog
from companion_memory.retrieval.semantic_schema import validate_work,INPUT_LEAF,VECTOR_LEAF,validate
from companion_memory.provider.embedding_schema import HANDOFF_LEAF,validate_leaf
from companion_memory.memory.semantic_schema import validate as memory_validate
from companion_memory.cognition.fixed_memory_schema import validate_set,validate_member

CONFIG={'database_id':'database','instance_id':'instance','snapshot_id':'snapshot'}
RECEIPT={'kind':'delete','key':'original','fingerprint':'a'*64}


def local_work():
    return {'v':1,'revision':1,'row_id':'local-row','work_id':'delete-work','config':CONFIG,'space_id':'space','kind':'DELETE_LOCAL',
        'state':'LOCAL_PREPARED','object_ref':{'object_id':'object','revision':2},'change_seq':1,'superseded_by_revision':None,
        'error':'NONE','cleanup_pending':False,'created_at':1,'completion_ref':None,'deletion_ref':RECEIPT}


def embed_work():
    result=local_work();result.pop('deletion_ref')
    result.update({'row_id':'embed-row','work_id':'embed-work','kind':'EMBED','state':'PREPARED','purpose':'DOCUMENT','partition_id':'partition',
        'material_digest':'b'*64,'input_leaf_count':3,'input_bytes':8192,'original_request_key':'provider-key','intent':None,
        'request_ref':None,'artifact_id':None,'deadline_at':None})
    return result


def catalogs():
    return semantic_memory_catalog(),semantic_retrieval_catalog(),embedding_handoff_catalog(),fixed_memory_catalog()


class RecordTests(unittest.TestCase):
    def test_fifteen_tables_include_full_schema_and_per_table_capacity(self):
        tables=[table for c in catalogs() for table in c.definition.tables if table.sql.startswith('CREATE TABLE')]
        self.assertEqual(len(tables),15)
        sizes={}
        for table in tables:
            self.assertTrue(table.record_schemas)
            size=len(encode_value(table_descriptor(table),32768))
            self.assertLessEqual(size,32768);sizes[table.name]=size
        print(json.dumps({'table_declaration_bytes':sizes,'table_declaration_sum':sum(sizes.values())},sort_keys=True))

    def test_local_branch_rejects_all_network_fields_and_false_success(self):
        value=local_work();self.assertEqual(validate_work(value)['state'],'LOCAL_PREPARED')
        for key in ('purpose','partition_id','material_digest','input_leaf_count','input_bytes','original_request_key','intent','request_ref','artifact_id','deadline_at'):
            with self.subTest(key=key),self.assertRaises(InvalidValue):validate_work({**value,key:None})
        for state in ('LOCAL_APPLIED','LOCAL_SUPERSEDED','LOCAL_FAILED'):
            with self.assertRaises(InvalidValue):validate_work({**value,'state':state})
            self.assertEqual(validate_work({**value,'state':state,'completion_ref':RECEIPT})['state'],state)
        for state in ('RESULT_STORED','APPLIED'):
            with self.assertRaises(InvalidValue):validate_work({**embed_work(),'state':state,'completion_ref':RECEIPT})
        with self.assertRaises(InvalidValue):validate_work({**value,'change_seq':None})
        with self.assertRaises(InvalidValue):validate_work({**value,'object_ref':None})

    def test_leaf_byte_capacity_and_missing_or_changed_payload(self):
        sizes={}
        for schema,domain,parent,raw,validator in ((INPUT_LEAF,'embedding-input','work_id',b'\x00'*3072,lambda v:validate('embedding_input_leaf',v)),
            (VECTOR_LEAF,'embedding-vector','artifact_id',b'\xff'*4096,lambda v:validate('embedding_vector_leaf',v)),
            (HANDOFF_LEAF,'embedding-handoff','handoff_id',b'\xff'*4096,validate_leaf)):
            refs={'request_id':'r'*128,'attempt_id':'a'*128} if schema is HANDOFF_LEAF else {}
            leaf=make_leaf(schema,domain,parent,'p'*128,0,raw,**refs);validator(leaf)
            self.assertLessEqual(len(encode_content(leaf,8192)),8192);sizes[domain]=len(encode_content(leaf,8192))
            with self.assertRaises(InvalidValue):validator({**leaf,'byte_count':True})
            encoded=leaf['data_base64'];assert type(encoded) is str
            with self.assertRaises(InvalidValue):validator({**leaf,'data_base64':encoded+'\n'})
            with self.assertRaises(InvalidValue):validator({**leaf,'digest':'0'*64})
        print(json.dumps({'maximum_leaf_metadata_bytes':sizes}))

    def test_sql_unique_and_embed_only_input_foreign_key_survive_reopen(self):
        with tempfile.TemporaryDirectory() as root:
            path=root+'/records.sqlite3';db=sqlite3.connect(path);db.execute('PRAGMA foreign_keys=ON')
            for c in catalogs():
                for table in c.definition.tables:db.execute(table.sql)
            def insert(table,value):
                db.execute('INSERT INTO '+table+'(scope_id,row_id,revision,body) VALUES(?,?,?,?)',
                    ('instance',value['row_id'],value['revision'],encode_content(value,8192).decode()))
            local=validate_work(local_work());insert('retrieval_embedding_work',local);db.commit()
            leaf=make_leaf(INPUT_LEAF,'embedding-input','work_id','delete-work',0,b'no local input')
            with self.assertRaises(sqlite3.IntegrityError):insert('retrieval_embedding_input_leaf',leaf)
            db.rollback()
            embed=validate_work(embed_work());insert('retrieval_embedding_work',embed)
            leaf=make_leaf(INPUT_LEAF,'embedding-input','work_id','embed-work',0,b'x'*3072);insert('retrieval_embedding_input_leaf',leaf);db.commit()
            with self.assertRaises(sqlite3.IntegrityError):insert('retrieval_embedding_work',validate_work({**embed,'row_id':'another-row','work_id':'another-work'}))
            db.rollback();db.close();db=sqlite3.connect(path)
            self.assertEqual(db.execute('SELECT count(*) FROM retrieval_embedding_work').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM retrieval_embedding_input_leaf').fetchone()[0],1)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok');db.close()

    def test_ack_and_partial_fixed_set_shape(self):
        ack={'v':1,'row_id':'ack','revision':2,'space_id':'space','object_id':'object','object_revision':2,
            'resolved_from_seq':11,'applied_seq':13,'action':'DELETE','material_digest':None,'artifact_id':None,'evidence_receipt':RECEIPT}
        memory_validate('semantic_ack',ack)
        with self.assertRaises(InvalidValue):memory_validate('semantic_ack',{**ack,'artifact_id':'artifact'})
        value={'v':1,'row_id':'set','revision':1,'set_id':'set','config':CONFIG,'state':'SEALED','expected_members':12,
            'stored_members':12,'established_members':5,'manifest_digest':'a'*64,'review_ref':'review','review_digest':'b'*64,
            'reviewed_by':'synthetic-fixture-reviewer','created_at':1}
        for count in (5,6,11):self.assertEqual(validate_set({**value,'established_members':count})['state'],'SEALED')
        with self.assertRaises(InvalidValue):validate_set({**value,'state':'ESTABLISHED'})
        self.assertEqual(validate_set({**value,'state':'ESTABLISHED','established_members':12})['state'],'ESTABLISHED')
