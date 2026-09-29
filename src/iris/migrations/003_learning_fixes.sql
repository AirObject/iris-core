ALTER TABLE model_calls ADD COLUMN finish_reason TEXT;
ALTER TABLE model_calls ADD COLUMN batch_id INTEGER REFERENCES batches(id);
CREATE INDEX model_calls_by_batch ON model_calls(batch_id);

-- Old aliases have no learning provenance; do not invent a source for them.
ALTER TABLE subject_aliases ADD COLUMN source_message_id INTEGER REFERENCES messages(id);
