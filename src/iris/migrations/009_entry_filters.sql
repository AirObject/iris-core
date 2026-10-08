-- Admission is independent of learning outcome; existing entries keep all inputs.
ALTER TABLE entries ADD COLUMN filters_json TEXT NOT NULL DEFAULT '{"min_chars":0,"mention_only":false,"context_messages":0,"max_batches_per_hour":0}';
ALTER TABLE entries ADD COLUMN message_sequence INTEGER NOT NULL DEFAULT 0;
-- Keep internal admission metadata out of the existing recent-message projection.
-- Durable raw positions prevent cleanup from making distant messages adjacent.
CREATE TABLE message_admission (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    entry_id TEXT NOT NULL REFERENCES entries(id),
    position INTEGER NOT NULL,
    decided INTEGER NOT NULL DEFAULT 0 CHECK(decided IN (0,1)),
    reason TEXT,
    UNIQUE(entry_id,position)
);
-- Already frozen context keeps its admission when it becomes a target.
INSERT INTO message_admission(message_id,entry_id,position,decided)
SELECT m.id,m.entry_id,row_number() OVER (PARTITION BY m.entry_id ORDER BY m.id),
    m.learning_state!='pending' OR EXISTS(SELECT 1 FROM batch_message_refs r WHERE r.message_id=m.id)
FROM messages m;
UPDATE entries SET message_sequence=COALESCE((SELECT MAX(position) FROM message_admission WHERE entry_id=entries.id),0);
CREATE INDEX message_admission_undecided ON message_admission(entry_id,message_id) WHERE decided=0;
CREATE INDEX batches_entry_created ON batches(entry_id,julianday(created_at));
-- NULL means this historical batch predates settings snapshots.
ALTER TABLE batches ADD COLUMN entry_settings_json TEXT;
