-- Historical calls have unknown effort/field diagnostics, not inferred defaults.
ALTER TABLE model_calls ADD COLUMN reasoning_effort TEXT;
ALTER TABLE model_calls ADD COLUMN reasoning_present INTEGER CHECK (reasoning_present IN (0,1));
ALTER TABLE model_calls ADD COLUMN reasoning_chars INTEGER CHECK (reasoning_chars >= 0);
