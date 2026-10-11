-- Bytes are shared by hash; interpretations belong to individual media objects.
CREATE TABLE media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE CHECK(length(sha256)=64),
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes>0),
    created_at TEXT NOT NULL,
    unreferenced_at TEXT,
    refused_at TEXT
);
CREATE INDEX media_files_orphans ON media_files(unreferenced_at) WHERE unreferenced_at IS NOT NULL;
CREATE TABLE media_objects (
    id TEXT PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('image','audio','video')),
    understanding_source TEXT NOT NULL CHECK(understanding_source IN ('host','system','refused','unprocessed')),
    understanding_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    last_error TEXT,
    attempt_token TEXT,
    lease_until TEXT,
    CHECK(understanding_source!='refused' OR understanding_text='敏感信息无法访问')
);
CREATE INDEX media_objects_file ON media_objects(file_id);
CREATE TABLE message_media (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position>=0),
    media_id TEXT NOT NULL REFERENCES media_objects(id),
    PRIMARY KEY(message_id,position),
    UNIQUE(message_id,media_id)
);
CREATE INDEX message_media_object ON message_media(media_id);
CREATE TRIGGER message_media_attached AFTER INSERT ON message_media BEGIN
    UPDATE media_files SET unreferenced_at=NULL
    WHERE id=(SELECT file_id FROM media_objects WHERE id=new.media_id);
END;
-- Covers daily maintenance and explicit source-message removal alike.
CREATE TRIGGER message_media_detached AFTER DELETE ON message_media BEGIN
    UPDATE media_files SET unreferenced_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
    WHERE id=(SELECT file_id FROM media_objects WHERE id=old.media_id)
    AND NOT EXISTS(SELECT 1 FROM media_objects o JOIN message_media r ON r.media_id=o.id
                   WHERE o.file_id=media_files.id);
END;
-- Append the media phase without changing any existing phase number/cursor.
ALTER TABLE maintenance_runs ADD COLUMN media_through INTEGER NOT NULL DEFAULT 0;
