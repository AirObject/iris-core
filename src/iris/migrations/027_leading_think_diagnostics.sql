-- Older calls have unknown prefix diagnostics; do not infer their response format.
ALTER TABLE model_calls ADD COLUMN leading_think_stripped INTEGER
    CHECK (leading_think_stripped IN (0, 1));
