-- iris: online_safe=true lock_ms=100 min_app=0.13.0 max_app= recovery=none
-- Bounded Task deletion inventories must not scan all event delivery history.
CREATE INDEX idx_events_task_deletion ON cognitive_events
    (tenant_id,object_type,object_id,status,id);
CREATE INDEX idx_tasks_parent_deletion ON tasks (tenant_id,parent_task_id,id);
CREATE INDEX idx_task_step_revisions_task_deletion ON task_step_revisions (task_id,step_id);
CREATE INDEX idx_task_trigger_revisions_task_deletion ON task_trigger_revisions (task_id,trigger_id);
CREATE INDEX idx_task_triggers_parent_deletion ON task_triggers (tenant_id,task_id,id);
