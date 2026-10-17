-- Keep metadata after byte cleanup; pending intent survives an unlink/commit crash.
ALTER TABLE media_files ADD COLUMN bytes_purged_at TEXT;
ALTER TABLE media_files ADD COLUMN bytes_purge_pending INTEGER NOT NULL DEFAULT 0 CHECK(bytes_purge_pending IN (0,1));
CREATE INDEX media_files_retention ON media_files(created_at) WHERE bytes_purged_at IS NULL;
