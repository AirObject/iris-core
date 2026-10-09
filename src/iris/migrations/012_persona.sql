-- Persona keeps the existing content/is_current projection for M1 consumers.
ALTER TABLE persona_versions ADD COLUMN status TEXT NOT NULL DEFAULT 'history'
    CHECK(status IN ('current','pending','rejected','superseded','history'));
ALTER TABLE persona_versions ADD COLUMN source TEXT NOT NULL DEFAULT 'initial_setting'
    CHECK(source IN ('initial_setting','periodic','regenerate','admin_edit','rollback'));
ALTER TABLE persona_versions ADD COLUMN sentences_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE persona_versions ADD COLUMN checks_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE persona_versions ADD COLUMN change_degree TEXT NOT NULL DEFAULT 'small'
    CHECK(change_degree IN ('small','medium','large'));
ALTER TABLE persona_versions ADD COLUMN base_version_id INTEGER REFERENCES persona_versions(id);
ALTER TABLE persona_versions ADD COLUMN rollback_of INTEGER REFERENCES persona_versions(id);
ALTER TABLE persona_versions ADD COLUMN published_at TEXT;
ALTER TABLE persona_versions ADD COLUMN material_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE persona_versions ADD COLUMN self_snapshot_json TEXT;
UPDATE persona_versions SET status=CASE WHEN is_current=1 THEN 'current' ELSE 'history' END,
    published_at=created_at,
    base_version_id=(SELECT MAX(p.id) FROM persona_versions p WHERE p.id<persona_versions.id);
CREATE UNIQUE INDEX persona_one_current ON persona_versions(is_current) WHERE is_current=1;
CREATE UNIQUE INDEX persona_one_pending ON persona_versions(status) WHERE status='pending';
CREATE TABLE persona_attempts (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    base_version_id INTEGER NOT NULL REFERENCES persona_versions(id),
    state TEXT NOT NULL,
    reason TEXT,
    version_id INTEGER REFERENCES persona_versions(id),
    material_json TEXT NOT NULL,
    outputs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
INSERT OR IGNORE INTO runtime_settings(key,value_json) VALUES('persona_publish_mode','"small_medium_auto"');

-- Freeze identifiable pre-M3 setting provenance at upgrade time. Reading a
-- version later must not silently rebind its evidence to edited memory text.
CREATE TEMP TABLE persona_legacy_settings AS
SELECT m.id,m.created_at,
    COALESCE((SELECT json_extract(r.before_json,'$.content') FROM memory_revisions r
              WHERE r.memory_id=m.id AND r.revision_before=1 ORDER BY r.id LIMIT 1),m.content) AS content,
    (SELECT MIN(r.created_at) FROM memory_revisions r WHERE r.memory_id=m.id
     AND json_extract(r.after_json,'$.lifecycle')='deleted') AS deleted_at
FROM memories m WHERE EXISTS(SELECT 1 FROM sources s WHERE s.memory_id=m.id AND s.kind='initial_setting');
UPDATE persona_versions SET sentences_json=json_array(
    json_object('text',substr(content,1,instr(content,'。')),'origin','initial_template',
        'basis',json('[]'),'dates',json('[]'),'date_count',0,'initial_setting',json('true'),
        'initial_settings',json_object('template_text',substr(content,1,instr(content,'。')))),
    json_object('text',substr(content,instr(content,'。')+1),'origin','initial_template',
        'basis',json((SELECT json_group_array(json_object('memory_id',s.id,'revision',1))
            FROM persona_legacy_settings s WHERE s.content!='' AND instr(persona_versions.content,s.content)>0
            -- M1 inserted persona before its setting memories in the same transaction.
            -- The next version, rather than this version's timestamp, bounds that setup.
            AND julianday(s.created_at)<=julianday(COALESCE(
                (SELECT p.created_at FROM persona_versions p WHERE p.id>persona_versions.id ORDER BY p.id LIMIT 1),
                '9999-12-31'))
            AND (s.deleted_at IS NULL OR julianday(s.deleted_at)>julianday(persona_versions.created_at)))),
        'dates',json('[]'),'date_count',0,'initial_setting',json('true'),
        'initial_settings',json_object('template_text',substr(content,instr(content,'。')+1)))
    ), checks_json='{"passed":true,"legacy":true,"warning":"Pre-M3 template: identifiable setting memories recovered; other provenance is unknown."}'
WHERE sentences_json='[]';
DROP TABLE persona_legacy_settings;
