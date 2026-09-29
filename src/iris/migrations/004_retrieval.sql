CREATE INDEX memories_dedupe ON memories(speaker_subject_id,kind,stance,event_time,lifecycle);
CREATE INDEX memory_subjects_by_subject ON memory_subjects(subject_id,memory_id);
CREATE INDEX memories_lifecycle_importance ON memories(lifecycle,importance DESC,id);
CREATE INDEX memories_event_time ON memories(event_time);
CREATE INDEX sources_by_memory ON sources(memory_id);
CREATE INDEX messages_recent ON messages(entry_id,id DESC);
CREATE INDEX model_calls_latest ON model_calls(purpose,id DESC);
CREATE INDEX memory_gaps_entry ON memory_gaps(entry_id);

CREATE VIRTUAL TABLE memory_fts_jieba USING fts5(content,tags,tokenize='unicode61');
CREATE VIRTUAL TABLE memory_fts_trigram USING fts5(content,tags,tokenize='trigram');
INSERT INTO memory_fts_jieba(rowid,content,tags)
    SELECT m.id,iris_terms(m.content),iris_terms(COALESCE((SELECT group_concat(tag,' ') FROM memory_tags WHERE memory_id=m.id),''))
    FROM memories m WHERE m.lifecycle!='deleted';
INSERT INTO memory_fts_trigram(rowid,content,tags)
    SELECT m.id,m.content,COALESCE((SELECT group_concat(tag,' ') FROM memory_tags WHERE memory_id=m.id),'')
    FROM memories m WHERE m.lifecycle!='deleted';

-- Coalesced, durable notifications; consumed only after COMMIT. They never contain vectors.
CREATE TABLE vector_dirty(memory_id INTEGER PRIMARY KEY);
CREATE TRIGGER memory_insert_index AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts_jieba(rowid,content,tags) SELECT new.id,iris_terms(new.content),'' WHERE new.lifecycle!='deleted';
    INSERT INTO memory_fts_trigram(rowid,content,tags) SELECT new.id,new.content,'' WHERE new.lifecycle!='deleted';
    INSERT OR IGNORE INTO vector_dirty VALUES(new.id);
END;
CREATE TRIGGER memory_update_text AFTER UPDATE OF content,lifecycle ON memories BEGIN
    DELETE FROM memory_fts_jieba WHERE rowid=old.id;
    DELETE FROM memory_fts_trigram WHERE rowid=old.id;
    INSERT INTO memory_fts_jieba(rowid,content,tags)
        SELECT new.id,iris_terms(new.content),iris_terms(COALESCE((SELECT group_concat(tag,' ') FROM memory_tags WHERE memory_id=new.id),'')) WHERE new.lifecycle!='deleted';
    INSERT INTO memory_fts_trigram(rowid,content,tags)
        SELECT new.id,new.content,COALESCE((SELECT group_concat(tag,' ') FROM memory_tags WHERE memory_id=new.id),'') WHERE new.lifecycle!='deleted';
END;
CREATE TRIGGER memory_invalidate_embedding AFTER UPDATE OF content ON memories
WHEN new.content!=old.content AND new.embedding IS old.embedding BEGIN
    UPDATE memories SET embedding=NULL,embedding_model=NULL WHERE id=new.id;
END;
CREATE TRIGGER memory_update_vector AFTER UPDATE OF content,revision,embedding,embedding_model,lifecycle ON memories BEGIN
    INSERT OR IGNORE INTO vector_dirty VALUES(new.id);
END;
CREATE TRIGGER memory_delete_index AFTER DELETE ON memories BEGIN
    DELETE FROM memory_fts_jieba WHERE rowid=old.id;
    DELETE FROM memory_fts_trigram WHERE rowid=old.id;
    INSERT OR IGNORE INTO vector_dirty VALUES(old.id);
END;
CREATE TRIGGER memory_tag_insert AFTER INSERT ON memory_tags BEGIN
    UPDATE memory_fts_jieba SET tags=iris_terms(COALESCE((SELECT group_concat(tag,' ') FROM memory_tags WHERE memory_id=new.memory_id),'')) WHERE rowid=new.memory_id;
    UPDATE memory_fts_trigram SET tags=COALESCE((SELECT group_concat(tag,' ') FROM memory_tags WHERE memory_id=new.memory_id),'') WHERE rowid=new.memory_id;
END;
CREATE TRIGGER memory_tag_delete AFTER DELETE ON memory_tags BEGIN
    UPDATE memory_fts_jieba SET tags=iris_terms(COALESCE((SELECT group_concat(tag,' ') FROM memory_tags WHERE memory_id=old.memory_id),'')) WHERE rowid=old.memory_id;
    UPDATE memory_fts_trigram SET tags=COALESCE((SELECT group_concat(tag,' ') FROM memory_tags WHERE memory_id=old.memory_id),'') WHERE rowid=old.memory_id;
END;

CREATE TABLE recalls(id TEXT PRIMARY KEY,entry_id TEXT,request_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE recall_items(
    recall_id TEXT NOT NULL REFERENCES recalls(id) ON DELETE CASCADE,
    memory_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    used_at TEXT,
    PRIMARY KEY(recall_id,memory_id)
);
CREATE INDEX recalls_created ON recalls(created_at);
