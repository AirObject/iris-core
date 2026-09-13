"""Actual SQLite declarations and physical constraints for all six owner tables.

Records are complete synthetic fixtures. The tests distinguish SQL integrity,
closed body validation and semantic owner publication; inserting fixture rows is
not an implementation of the public persona workflow.
"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from typing import cast
from companion_memory.cognition.text_context import context_catalog,freeze_context
from companion_memory.memory.initial_self import initial_self_catalog,INITIAL_SELF,input_digest,isolate_initial_self
from companion_memory.self_model.repository import persona_catalog
from companion_memory.self_model.formats import RUN,CANDIDATE,PUBLICATION,isolate_run,isolate_candidate,isolate_publication,candidate_digest
from companion_memory.persistence.text_records import stable_identity,isolate_record
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue,Value
from companion_memory.provider.values import freeze
from companion_memory.memory.formats import record
from tests.cognition.test_text_context import inputs


def records():
    context=freeze_context(*inputs())
    base={'format_version':1,'revision':1,'database_id':'database','instance_id':'instance','config_snapshot_id':'config','created_at_us':1}
    def identity(kind,*parts):return stable_identity(kind,'database','instance',*parts)
    operation={'owner_namespace':'memory','operation_kind':'register_initial_self','scope_id':'instance','operation_key':'input'}
    initial={**base,'object_id':identity('self-input'),'self_subject_id':'self','self_revision':1,'input_kind':'NO_PRESET',
        'body':'The operator chose no preset identity.','actor_ref':'operator','operation':operation,'input_origin':'SYNTHETIC_FIXTURE'}
    initial['input_digest']=input_digest(record(cast(Value,freeze(initial,8192,owned=True))))
    initial=isolate_initial_self(initial)
    run=isolate_run({**base,'object_id':identity('persona-run'),'input_id':initial['object_id'],'input_digest':initial['input_digest'],
        'self_subject_id':'self','self_revision':1,'generation':1,'state':'PREPARED','provider_operation_key':'provider-original','provider_request_id':None,
        'resolution_id':None,'publication_id':None,'mode_epoch':2,'prompt_ref':'prompt','schema_ref':'schema','transform_ref':'transform','account_id':'account','window_id':'window',
        'binding_digest':'b'*64,'original_operation':{**operation,'owner_namespace':'self_model','operation_kind':'prepare_initial_persona','operation_key':'prepare'},
        'last_operation':{**operation,'owner_namespace':'self_model','operation_kind':'prepare_initial_persona','operation_key':'prepare'},'updated_at_us':1})
    candidate=isolate_candidate({**base,'object_id':identity('persona-candidate',run['object_id'],1),'run_id':run['object_id'],'generation':1,
        'provider_operation_key':'provider-original','provider_request_id':None,'handoff_id':None,'terminal_receipt':{**operation,'owner_namespace':'self_model','operation_kind':'record_initial_persona_resolution','operation_key':'not-sent'},
        'resolution':'NOT_SENT','failure_reason':'NONE','text':None,'text_digest':None,'input_id':initial['object_id'],'input_digest':initial['input_digest'],
        'binding_digest':'b'*64,'review':'NOT_APPLICABLE','reviewed_by':None,'reviewed_at_us':None,'review_operation':None})
    publication=isolate_publication({**base,'object_id':identity('persona-publication'),'run_id':run['object_id'],'generation':1,'candidate_id':candidate['object_id'],
        'candidate_revision':2,'candidate_digest':candidate_digest(candidate),'input_id':initial['object_id'],'input_digest':initial['input_digest'],'self_subject_id':'self','self_revision':1,
        'provider_request_id':'request','handoff_id':'handoff','requested_model_id':'ark-code-latest','reported_model_id':'fixture-backend','resolved_model_id':None,
        'prompt_ref':'prompt','schema_ref':'schema','transform_ref':'transform','text':'Synthetic published text for physical schema checks only.','generated_at_us':1,'reviewed_by':'operator',
        'review_operation':{**operation,'operation_kind':'review_initial_persona'},'publication_operation':{**operation,'operation_kind':'publish_initial_persona'}})
    return {'memory_initial_self_inputs':initial,'self_model_initial_persona_runs':run,'self_model_initial_persona_candidates':candidate,
        'self_model_persona_publications':publication,'cognition_learning_contexts':context.manifest,'cognition_learning_context_leaves':context.leaves[0]}


class TextRecordCatalogTests(unittest.TestCase):
    def test_six_tables_fourteen_indexes_and_actual_unique_null_semantics(self):
        catalogs=(initial_self_catalog(),persona_catalog(),context_catalog())
        tables=tuple(table for catalog in catalogs for table in catalog.definition.tables)
        self.assertEqual(sum(table.sql.startswith('CREATE TABLE') for table in tables),6)
        self.assertEqual(len(tables),20)
        self.assertLessEqual(sum(len(catalog.statements) for catalog in catalogs),48)
        for table in tables:self.assertLessEqual(len(table.sql.encode()),2048)
        for catalog in catalogs:
            for _,statement in catalog.statements:self.assertLessEqual(len(statement.sql.encode()),1024)
        values=records()
        with tempfile.TemporaryDirectory(prefix='text-table-fixture-') as directory:
            path=Path(directory)/'actual.sqlite3'
            connection=sqlite3.connect(path)
            for table in tables:connection.execute(table.sql)
            def insert(table,value):
                body=encode_content(record(cast(Value,freeze(value,8192,owned=True))),8192).decode()
                connection.execute('INSERT INTO '+table+' VALUES(?,?,?,?)',('instance',value['object_id'],value['revision'],body))
            for table,value in values.items():insert(table,value)
            connection.commit()
            for table,value in values.items():
                with self.subTest(table=table),self.assertRaises(sqlite3.IntegrityError):insert(table,{**value,'object_id':'different'})
                connection.rollback()
            original=values['self_model_initial_persona_candidates']
            # Two absent Provider request IDs are distinct historical no-send
            # generations; non-null request identity remains unique.
            second={**original,'object_id':'second','generation':2,'provider_operation_key':'second-key'}
            insert('self_model_initial_persona_candidates',second)
            third={**original,'object_id':'third','generation':3,'provider_operation_key':'third-key','provider_request_id':'request'}
            insert('self_model_initial_persona_candidates',third);connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                insert('self_model_initial_persona_candidates',{**third,'object_id':'fourth','run_id':'other-run','provider_operation_key':'fourth-key'})
            connection.rollback();connection.close()
            connection=sqlite3.connect(path)
            for table,value in values.items():
                row=connection.execute('SELECT body FROM '+table+' WHERE object_id=?',(value['object_id'],)).fetchone()
                self.assertEqual(row[0],encode_content(value,8192).decode())
            self.assertEqual(connection.execute('SELECT count(*) FROM self_model_initial_persona_candidates').fetchone()[0],3)
            connection.close()
        print(json.dumps({'new_tables':6,'new_indexes':14,'ddl_text_bytes':sum(len(t.sql.encode()) for t in tables),
            'statement_count':sum(len(c.statements) for c in catalogs),'body_sizes':{k:len(encode_content(v,8192)) for k,v in values.items()}},sort_keys=True))

    def test_closed_records_reject_extra_fields_and_wrong_review_state(self):
        values=records()
        for name,schema,maximum in (('memory_initial_self_inputs',INITIAL_SELF,8192),('self_model_initial_persona_runs',RUN,4096),
                ('self_model_initial_persona_candidates',CANDIDATE,4096),('self_model_persona_publications',PUBLICATION,4096)):
            with self.subTest(name=name),self.assertRaises(InvalidValue):isolate_record(schema,{**values[name],'unexpected':None},maximum)
        with self.assertRaises(InvalidValue):isolate_candidate({**values['self_model_initial_persona_candidates'],'review':'APPROVED'})
        with self.assertRaises(InvalidValue):isolate_run({**values['self_model_initial_persona_runs'],'state':'PUBLISHED'})
