"""Restartable daily maintenance with a separate, optional model phase."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import dumps
from .memory_ops import adjust_retention, delete_memory, lifecycle_settings, message_references, missing_batch_targets, operation
from .model_health import utc_now
from .media import GRACE_PERIOD, cleanup_upload_orphans, delete_unreferenced_file, files_for_message, mark_unreferenced
from .queue import reset_batch
from .runtime_journal import cleanup, maintenance_span, record

PHASES = ("decay", "expiry", "dependency", "messages", "retry", "consolidation", "persona", "goals", "media", "retention")


class Maintenance:
    def __init__(self, store, *, gateway=None, clock=utc_now):
        self.store, self.clock, self.gateway = store, clock, gateway
        self.started_at = self._last_poll = clock()
        with store.read() as conn:
            completed = conn.execute("""SELECT finished_at FROM maintenance_runs WHERE state='completed'
                ORDER BY julianday(finished_at) DESC LIMIT 1""").fetchone()
        self._startup_catchup_done = bool(completed and self.started_at-datetime.fromisoformat(completed[0]) <= timedelta(hours=24))
        self._lock = threading.Lock()
        self._journal_schedule = None

    def observe_schedule(self):
        """Record the next daily slot even while another maintenance is running."""
        try:
            current = self.clock()
            with self.store.read() as conn:
                config = lifecycle_settings(conn)
                zone_row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='timezone'").fetchone()
            zone = ZoneInfo(json.loads(zone_row[0]) if zone_row else 'Asia/Shanghai')
            hour, minute = map(int, config['maintenance_time'].split(':'))
            scheduled = current.astimezone(zone).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if scheduled <= current:
                scheduled += timedelta(days=1)
            if scheduled != self._journal_schedule:
                if self._journal_schedule and self._journal_schedule > current:
                    record(self.store, 'maintenance_skipped', current=current, phase='maintenance',
                           scheduled_at=self._journal_schedule, reason='schedule_changed')
                record(self.store, 'maintenance_scheduled', current=current, phase='maintenance', scheduled_at=scheduled)
                self._journal_schedule = scheduled
        except Exception:
            logging.getLogger('iris.runtime_journal').warning('runtime journal write failed')

    def due(self):
        """One most-recent crossed schedule slot, or the startup idle catchup."""
        current = self.clock()
        previous, self._last_poll = self._last_poll, current
        with self.store.read() as conn:
            config = lifecycle_settings(conn)
            zone_row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='timezone'").fetchone()
            zone = ZoneInfo(json.loads(zone_row[0]) if zone_row else "Asia/Shanghai")
            local = current.astimezone(zone)
            hour, minute = map(int, config["maintenance_time"].split(":"))
            scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if scheduled > current:
                scheduled -= timedelta(days=1)
            key = scheduled.date().isoformat()
            if previous < scheduled <= current and not conn.execute(
                    "SELECT 1 FROM maintenance_runs WHERE schedule_key=?", (key,)).fetchone():
                return "scheduled", key
            latest_row = conn.execute("""SELECT finished_at FROM maintenance_runs WHERE state='completed'
                ORDER BY julianday(finished_at) DESC LIMIT 1""").fetchone()
            latest = latest_row[0] if latest_row else None
            if latest and datetime.fromisoformat(latest) >= self.started_at:
                self._startup_catchup_done = True
            if self._startup_catchup_done or (latest and current-datetime.fromisoformat(latest) <= timedelta(hours=24)):
                return None
            latest_message = conn.execute("SELECT MAX(julianday(received_at)) FROM messages").fetchone()[0]
            quiet_after = self.started_at
            if latest_message is not None:
                # Compare instants, not lexical offsets in imported message timestamps.
                quiet_after = max(quiet_after, datetime.fromtimestamp((latest_message-2440587.5)*86400, tz=current.tzinfo))
            if current-quiet_after >= timedelta(minutes=10):
                # A catchup starting after today's slot consumes that slot too.
                if scheduled.date() == local.date():
                    if conn.execute("SELECT 1 FROM maintenance_runs WHERE schedule_key=?", (key,)).fetchone():
                        return None
                    return "catchup", key
                return "catchup", None
        return None

    def pending(self):
        with self.store.read() as conn:
            row = conn.execute("SELECT id FROM maintenance_runs WHERE state='running'").fetchone()
            return row[0] if row else None

    def request(self, *, trigger="manual", schedule_key=None, actor=None):
        with self.store.write() as conn:
            if trigger == 'catchup' and schedule_key is None:
                config = lifecycle_settings(conn)
                zone_row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='timezone'").fetchone()
                local = self.clock().astimezone(ZoneInfo(json.loads(zone_row[0]) if zone_row else 'Asia/Shanghai'))
                hour, minute = map(int, config['maintenance_time'].split(':'))
                if local >= local.replace(hour=hour, minute=minute, second=0, microsecond=0):
                    schedule_key = local.date().isoformat()
            row = conn.execute("SELECT id FROM maintenance_runs WHERE state='running'").fetchone()
            if row is None and schedule_key:
                row = conn.execute("SELECT id FROM maintenance_runs WHERE schedule_key=?", (schedule_key,)).fetchone()
            if row:
                rid = row[0]
            else:
                config = lifecycle_settings(conn)
                from .consolidation import consolidation_settings
                model_config = consolidation_settings(conn)
                zone_row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='timezone'").fetchone()
                bounds = [conn.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()[0]
                          for table in ("memories", "messages", "batches")]
                rid = conn.execute("""INSERT INTO maintenance_runs(trigger,schedule_key,settings_json,timezone,
                    memory_through,message_through,batch_through,created_at,goal_through) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (trigger, schedule_key, dumps(config), json.loads(zone_row[0]) if zone_row else "Asia/Shanghai",
                     *bounds, self.clock().isoformat(), conn.execute("SELECT COALESCE(MAX(id),0) FROM goals").fetchone()[0])).lastrowid
                conn.execute('UPDATE maintenance_runs SET media_through=(SELECT COALESCE(MAX(id),0) FROM media_files) WHERE id=?', (rid,))
                conn.execute('INSERT INTO consolidation_runs(run_id,settings_json) VALUES(?,?)', (rid,dumps(model_config)))
            if actor:
                operation(conn, "maintenance_requested", "maintenance", rid, actor=actor, stamp=self.clock().isoformat())
        if trigger in ("manual", "scheduled", "catchup"):
            self._startup_catchup_done = True
        return rid

    def run(self, run_id, *, stop=None):
        with self._lock:
            with self.store.read() as conn:
                row = conn.execute('SELECT * FROM maintenance_runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row['state'] == 'completed':
                return
            scheduled = None
            try:
                if row['schedule_key']:
                    scheduled = datetime.fromisoformat(row['schedule_key']+'T'+json.loads(row['settings_json'])['maintenance_time']).replace(tzinfo=ZoneInfo(row['timezone']))
            except Exception:
                logging.getLogger('iris.runtime_journal').warning('runtime journal write failed')
            with maintenance_span(self.store, run_id, 'maintenance', clock=self.clock, stop=stop, scheduled_at=scheduled):
                self._run(run_id, stop=stop)

    def _run(self, run_id, *, stop=None):
        # Progress is committed with each item, even when it needs no audit row.
        # The persisted cursor also rejects duplicate/stale worker invocations.
        while not (stop and stop()):
            with self.store.read() as conn:
                row = conn.execute("SELECT * FROM maintenance_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            run = dict(row)
            if run["state"] == "completed":
                return
            if run["phase"] == len(PHASES):
                self._finish(run_id)
                return
            phase = PHASES[run["phase"]]
            if phase == 'retention':
                if not self._retention(run, stop=stop):
                    return
                with self.store.write() as conn:
                    conn.execute("UPDATE maintenance_runs SET phase=phase+1,cursor_id=0 WHERE id=? AND phase=?", (run_id, run['phase']))
                continue
            if phase in ("consolidation", "persona"):
                from .consolidation import Consolidation, update_persona_for_run
                with maintenance_span(self.store, run_id, phase, clock=self.clock, stop=stop):
                    if phase == "consolidation":
                        if not Consolidation(self.store, self.gateway, clock=self.clock).run(run, stop=stop):
                            return
                    else:
                        try:
                            update_persona_for_run(self.store, self.gateway, run, clock=self.clock, stop=stop)
                        except Exception as exc:
                            self._persona_failure(run, exc)
                        if stop and stop():
                            return
                with self.store.write() as conn:
                    conn.execute("UPDATE maintenance_runs SET phase=phase+1,cursor_id=0 WHERE id=? AND phase=?", (run_id, run["phase"]))
                continue
            candidates = self._candidates(run, phase)
            if not candidates:
                if phase == "media":
                    orphans = cleanup_upload_orphans(self.store, self.clock())
                    if any(orphans.values()):
                        with self.store.write() as conn:
                            operation(conn, "media_upload_cleanup", "maintenance", run_id, orphans,
                                      actor="system", stamp=self.clock().isoformat())
                with self.store.write() as conn:
                    conn.execute("UPDATE maintenance_runs SET phase=phase+1,cursor_id=0 WHERE id=? AND phase=?", (run_id, run["phase"]))
                continue
            for candidate in candidates:
                if stop and stop():
                    return
                self._process_item(run, phase, candidate)

    def _persona_failure(self, run, error):
        # Persona is one atomic generation/check item. Even failure during due
        # selection must leave a terminal checkpoint before moving on to goals.
        details={'error_type':type(error).__name__, 'status':'failed'}
        with self.store.write() as conn:
            row=conn.execute('SELECT * FROM maintenance_persona WHERE run_id=?',(run['id'],)).fetchone()
            if row and row['status']!='running':
                return
            attempt_id=row['attempt_id'] if row else None
            if attempt_id:
                conn.execute("""UPDATE persona_attempts SET state='failed',reason='unexpected_error',finished_at=?
                    WHERE id=? AND state IN ('queued','running')""",(self.clock().isoformat(),attempt_id))
            conn.execute("""INSERT INTO maintenance_persona(run_id,status,reason,details_json)
                VALUES(?,'failed','unexpected_error',?) ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status,reason=excluded.reason,details_json=excluded.details_json""",(run['id'],dumps(details)))
            conn.execute("""INSERT OR IGNORE INTO maintenance_items
                (run_id,phase,item_key,object_id,outcome,reason,details_json,created_at)
                VALUES(?,'persona','update',?,'failed','unexpected_error',?,?)""",
                (run['id'],attempt_id or run['id'],dumps(details),self.clock().isoformat()))

    def _candidates(self, run, phase):
        config = json.loads(run["settings_json"])
        current = datetime.fromisoformat(run["created_at"])
        if phase == "media":
            with self.store.read() as conn:
                return [dict(r) for r in conn.execute("""SELECT id,id AS cursor_id,CAST(id AS TEXT) AS item_key
                    FROM media_files WHERE id>? AND id<=? AND unreferenced_at IS NOT NULL
                    AND julianday(unreferenced_at)<=julianday(?) ORDER BY id LIMIT 128""",
                    (run['cursor_id'], run['media_through'], (current-GRACE_PERIOD).isoformat()))]
        if phase == "goals":
            from .consolidation import run_settings
            with self.store.read() as conn:
                settings = run_settings(conn, run['id'])
                if not settings['enabled'] or not settings['goal_review_enabled']:
                    return []
                return [dict(r) for r in conn.execute("""SELECT id,id AS cursor_id,revision,CAST(id AS TEXT) AS item_key
                    FROM goals WHERE id>? AND id<=? AND state='open' AND merged_into IS NULL ORDER BY id LIMIT 100""",
                    (run['cursor_id'],run['goal_through']))]
        if phase in ("decay", "expiry"):
            where = "m.id>? AND m.id<=? AND m.lifecycle!='deleted'"
            params = [run["cursor_id"], run["memory_through"]]
            if phase == "expiry":
                if not config["auto_delete_enabled"]:
                    return []
                where += " AND m.lifecycle='forgotten' AND julianday(m.forgotten_at)<=julianday(?)"
                params.append((current-timedelta(days=config["auto_delete_days"])).isoformat())
            select = "SELECT m.id,m.id AS cursor_id,m.revision,CAST(m.id AS TEXT) AS item_key FROM memories m WHERE " + where
        elif phase == "dependency":
            # Loss rows are never deleted. rowid visits newly generated cascades
            # even when their child ID is lower than the previous child.
            select = """SELECT d.memory_id AS id,d.rowid AS cursor_id,m.revision,d.source_memory_id,
                CAST(d.memory_id AS TEXT)||':'||CAST(d.source_memory_id AS TEXT) AS item_key
                FROM memory_dependency_losses d JOIN memories m ON m.id=d.memory_id
                WHERE d.rowid>? AND d.applied_at IS NULL AND d.memory_id<=?"""
            params = [run["cursor_id"], run["memory_through"]]
        elif phase == "messages":
            select = """SELECT id,id AS cursor_id,CAST(id AS TEXT) AS item_key FROM messages
                WHERE id>? AND id<=? AND learning_state IN ('learned','abandoned','refused','filtered')
                AND julianday(received_at)<julianday(?)"""
            params = [run["cursor_id"], run["message_through"], (current-timedelta(days=config["message_retention_days"])).isoformat()]
        else:
            if not config["abandoned_retry_enabled"]:
                return []
            date = current.astimezone(ZoneInfo(run["timezone"])).replace(hour=0, minute=0, second=0, microsecond=0)
            select = """SELECT id,id AS cursor_id,CAST(id AS TEXT) AS item_key FROM batches b
                WHERE id>? AND id<=? AND state='abandoned'
                AND julianday(finished_at)>=julianday(?) AND julianday(finished_at)<julianday(?)
                AND NOT EXISTS(SELECT 1 FROM maintenance_batch_retries r WHERE r.batch_id=b.id)"""
            params = [run["cursor_id"], run["batch_through"], (date-timedelta(days=1)).isoformat(), date.isoformat()]
        with self.store.read() as conn:
            return [dict(r) for r in conn.execute(f"SELECT c.* FROM ({select}) c ORDER BY c.cursor_id LIMIT 128", params)]

    def _progress(self, conn, run, candidate):
        row = conn.execute("SELECT state,phase,cursor_id,progress_json FROM maintenance_runs WHERE id=?", (run["id"],)).fetchone()
        if row["state"] != "running" or row["phase"] != run["phase"] or row["cursor_id"] >= candidate["cursor_id"]:
            return None
        return json.loads(row["progress_json"])

    def _record(self, conn, run, phase, candidate, progress, outcome, reason, details):
        checks = progress.setdefault("checks", {})
        checks[phase] = checks.get(phase, 0) + 1
        if outcome == "skipped":
            reasons = progress.setdefault("skipped", {})
            reasons[reason] = reasons.get(reason, 0) + 1
        # Bookkeeping (counter advancement/cursor) is not a user-visible memory
        # change. No per-object row for checked, pinned, referenced or conflicts.
        if outcome not in ("checked", "skipped") or details.get("transition"):
            conn.execute("""INSERT INTO maintenance_items
                (run_id,phase,item_key,memory_id,object_id,outcome,reason,details_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (run["id"], phase, candidate["item_key"],
                candidate["id"] if phase in ("decay", "expiry", "dependency") else None,
                candidate["id"], outcome, reason, dumps(details), self.clock().isoformat()))
        conn.execute("UPDATE maintenance_runs SET cursor_id=?,progress_json=? WHERE id=?",
                     (candidate["cursor_id"], dumps(progress), run["id"]))

    def _process_item(self, run, phase, candidate):
        try:
            with self.store.write() as conn:
                progress = self._progress(conn, run, candidate)
                if progress is None:
                    return
                outcome, reason, details = self._apply(conn, run, phase, candidate)
                self._record(conn, run, phase, candidate, progress, outcome, reason, details)
        except Exception as exc:
            # Mutation and cursor both rolled back. Record only a safe failure
            # category and advance atomically, never private exception text.
            with self.store.write() as conn:
                progress = self._progress(conn, run, candidate)
                if progress is not None:
                    self._record(conn, run, phase, candidate, progress, "failed", type(exc).__name__, {})

    def _apply(self, conn, run, phase, candidate):
        config = json.loads(run["settings_json"])
        current = datetime.fromisoformat(run["created_at"])
        # Selection cutoffs belong to the persisted run, but a new lifecycle
        # transition starts its retention period at the actual execution time.
        executed_at = self.clock()
        mid = candidate["id"]
        if phase == "media":
            deleted = delete_unreferenced_file(self.store, conn, mid, current)
            return ("media_deleted", None, {}) if deleted else ("skipped", "media_referenced_or_not_due", {})
        if phase == "goals":
            from .goals import review_goal_basis, GoalConflict
            try:
                # Include the review's changes, report and cursor in this one
                # small transaction. The standalone paging helper cannot commit
                # our cursor/report atomically, so use its single-item API.
                result = review_goal_basis(conn, executed_at, goal_id=mid, expected_revision=candidate['revision'])
            except (GoalConflict, KeyError):
                return 'skipped', 'revision_conflict', {}
            return ('goals_reviewed' if result['changed_goal_ids'] else 'checked'), None, result
        if phase in ("decay", "expiry", "dependency"):
            memory = conn.execute("""SELECT id,lifecycle,revision,pinned,importance,retention,decay_visits,forgotten_at
                FROM memories WHERE id=?""", (mid,)).fetchone()
            if memory is None or memory["lifecycle"] == "deleted":
                return "skipped", "deleted", {}
            if memory["revision"] != candidate["revision"]:
                return "skipped", "revision_conflict", {}
            if memory["pinned"]:
                return "skipped", "pinned", {}
        if phase == "decay":
            if memory["lifecycle"] == "forgotten":
                if memory["retention"] >= config["restore_threshold"]:
                    # A changed H still takes effect; never decay or advance the
                    # three-visit counter while this item starts out forgotten.
                    adjust_retention(self.store, mid, current=executed_at, settings=config, _conn=conn)
                    return "restored", None, {"before": memory["retention"], "after": memory["retention"]}
                return "skipped", "forgotten", {}
            amount = 0
            if memory["importance"] < 40:
                amount = config["decay_amount"]
            elif memory["importance"] < 70:
                visits = (memory["decay_visits"]+1) % 3
                conn.execute("UPDATE memories SET decay_visits=? WHERE id=?", (visits, mid))
                if visits == 0:
                    amount = config["decay_amount"]
            if amount == 0 and memory["retention"] >= config["forget_threshold"]:
                return "checked", None, {}
            result = adjust_retention(self.store, mid, -amount, current=executed_at, settings=config, _conn=conn)
            details = {"before": memory["retention"], "after": result["retention"]}
            if result["lifecycle"] != memory["lifecycle"]:
                details["transition"] = result["lifecycle"]
            return ("decayed" if result["retention"] < memory["retention"] else "checked"), None, details
        if phase == "expiry":
            if memory["lifecycle"] != "forgotten" or not memory["forgotten_at"] or current-datetime.fromisoformat(memory["forgotten_at"]) < timedelta(days=config["auto_delete_days"]):
                return "skipped", "no_longer_due", {}
            delete_memory(self.store, mid, memory["revision"], actor="maintenance", current=executed_at, _conn=conn)
            return "deleted", None, {}
        if phase == "dependency":
            parent = candidate["source_memory_id"]
            loss = conn.execute("SELECT applied_at FROM memory_dependency_losses WHERE memory_id=? AND source_memory_id=?", (mid, parent)).fetchone()
            if loss is None or loss[0] is not None:
                return "skipped", "already_applied", {}
            # A removed evidence link no longer represents a dependency.
            if not conn.execute("SELECT 1 FROM sources WHERE kind='memory' AND memory_id=? AND source_memory_id=?", (mid, parent)).fetchone():
                return "skipped", "dependency_removed", {}
            amount = config["dependency_penalty"]
            result = adjust_retention(self.store, mid, -amount, current=executed_at, settings=config, _conn=conn)
            conn.execute("UPDATE memory_dependency_losses SET applied_at=?,amount=?,run_id=? WHERE memory_id=? AND source_memory_id=?",
                         (self.clock().isoformat(), amount, run["id"], mid, parent))
            details = {"source_memory_id": parent, "before": memory["retention"], "after": result["retention"]}
            if result["lifecycle"] != memory["lifecycle"]:
                details["transition"] = result["lifecycle"]
            return ("dependencies_weakened" if result["retention"] < memory["retention"] else "checked"), None, details
        if phase == "messages":
            message = conn.execute("SELECT learning_state,received_at FROM messages WHERE id=?", (mid,)).fetchone()
            if not message or message["learning_state"] not in ("learned", "abandoned", "refused", "filtered") or current-datetime.fromisoformat(message["received_at"]) <= timedelta(days=config["message_retention_days"]):
                return "skipped", "no_longer_eligible", {}
            if config['abandoned_retry_enabled']:
                day = current.astimezone(ZoneInfo(run['timezone'])).replace(hour=0, minute=0, second=0, microsecond=0)
                retry = conn.execute("""SELECT 1 FROM batches b JOIN json_each(b.target_ids) target
                    WHERE b.state='abandoned' AND b.id<=? AND target.value=?
                    AND julianday(b.finished_at)>=julianday(?) AND julianday(b.finished_at)<julianday(?)
                    AND NOT EXISTS(SELECT 1 FROM maintenance_batch_retries r WHERE r.batch_id=b.id) LIMIT 1""",
                    (run['batch_through'], mid, (day-timedelta(days=1)).isoformat(), day.isoformat())).fetchone()
                if retry:
                    return 'skipped', 'batch_retry_pending', {}
            references = message_references(conn, mid)
            if references:
                return "skipped", "referenced", {"references": references}
            media_files = files_for_message(conn, mid)
            conn.execute("DELETE FROM messages WHERE id=?", (mid,))
            mark_unreferenced(conn, media_files, executed_at)
            return "messages_deleted", None, {}
        batch = conn.execute("SELECT state,finished_at FROM batches WHERE id=?", (mid,)).fetchone()
        if batch is None or batch["state"] != "abandoned":
            return "skipped", "batch_state_changed", {}
        if conn.execute("SELECT 1 FROM maintenance_batch_retries WHERE batch_id=?", (mid,)).fetchone():
            return "skipped", "already_retried", {}
        yesterday = current.astimezone(ZoneInfo(run["timezone"])).date()-timedelta(days=1)
        if not batch["finished_at"] or datetime.fromisoformat(batch["finished_at"]).astimezone(ZoneInfo(run["timezone"])).date() != yesterday:
            return "skipped", "batch_date_changed", {}
        if missing_batch_targets(conn, mid):
            return "skipped", "target_messages_cleared", {}
        reset_batch(self.store, mid, _conn=conn)
        conn.execute("INSERT INTO maintenance_batch_retries VALUES(?,?,?)", (mid, run["id"], self.clock().isoformat()))
        return "batches_retried", None, {}

    @staticmethod
    def _summary(items, progress):
        result = {name: {"count": 0, "memory_ids": [], "object_ids": []} for name in (
            "decayed", "checked", "forgotten", "restored", "deleted", "dependencies_weakened",
            "messages_deleted", "batches_retried", "skipped", "failed")}
        for item in items:
            names = [] if item["outcome"] in ("checked", "skipped") else [item["outcome"]]
            transition = item["details"].get("transition")
            if transition:
                names.append("forgotten" if transition == "forgotten" else "restored")
            for name in names:
                group = result.setdefault(name, {"count": 0, "memory_ids": [], "object_ids": []})
                group["count"] += 1
                for key, value in (("memory_ids", item["memory_id"]), ("object_ids", item["object_id"])):
                    if value is not None:
                        group[key].append(value)
        for group in result.values():
            group["memory_ids"] = sorted(set(group["memory_ids"]))
            group["object_ids"] = sorted(set(group["object_ids"]))
        by_phase = {phase: progress.get("checks", {}).get(phase, 0) for phase in PHASES if phase not in ("consolidation", "persona", "retention")}
        result["checked"].update(count=sum(by_phase.values()), by_phase=by_phase)
        reasons = progress.get("skipped", {})
        result["skipped"].update(count=sum(reasons.values()), reasons=reasons)
        return result

    def report(self, run_id):
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM maintenance_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            report = dict(row)
            report["settings"] = json.loads(report.pop("settings_json"))
            report.pop("summary_json")
            progress = json.loads(report.pop("progress_json"))
            from .consolidation import suggestion_review
            suggestions = {}
            for annotation in conn.execute("SELECT * FROM consolidation_annotations WHERE run_id=? ORDER BY id", (run_id,)):
                suggestions.setdefault(str(annotation['work_id']), []).append({
                    'id': annotation['id'], 'memory_id': annotation['memory_id'],
                    'kind': annotation['kind'], 'text': annotation['text'], **suggestion_review(annotation)})
            items = []
            for item in conn.execute("SELECT * FROM maintenance_items WHERE run_id=? ORDER BY rowid", (run_id,)):
                item = dict(item)
                item["details"] = json.loads(item.pop("details_json"))
                if item['phase']=='consolidation' and item['outcome'] in ('conflicts','dependencies_reviewed'):
                    item['details']['model_suggestion'] = True
                    item['details']['report'] = '模型建议：'+item['details'].get('report','')
                    item['details']['suggestions'] = suggestions.get(item['item_key'], [])
                items.append(item)
            report["items"] = items
            report["summary"] = self._summary(items, progress)
            model = conn.execute("SELECT * FROM consolidation_runs WHERE run_id=?", (run_id,)).fetchone()
            if model:
                rows = conn.execute("SELECT outcome,reason,details_json FROM consolidation_run_work WHERE run_id=?", (run_id,)).fetchall()
                reasons = report["summary"]["skipped"]["reasons"]
                for result in rows:
                    if result['outcome'] == 'skipped':
                        reasons[result['reason']] = reasons.get(result['reason'], 0) + 1
                if model['skip_reason']:
                    reasons[model['skip_reason']] = max(1, reasons.get(model['skip_reason'], 0))
                report["summary"]["skipped"]["count"] = sum(reasons.values())
                calls = conn.execute("""SELECT COUNT(*) AS count,COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens),0) AS completion_tokens,COALESCE(SUM(duration_ms),0) AS duration_ms,
                    COALESCE(SUM(prompt_tokens IS NULL OR completion_tokens IS NULL),0) AS unknown_usage_calls
                    FROM consolidation_calls WHERE run_id=?""", (run_id,)).fetchone()
                report['summary']['model_calls'] = dict(calls)
                report['consolidation'] = {'settings': json.loads(model['settings_json']),
                    'deferred': sum((r['outcome'] is None or r['outcome'] in ('failed','skipped'))
                        and r['reason']!='unsafe_write' and not json.loads(r['details_json']).get('terminal',False) for r in rows),
                    'skip_reason': model['skip_reason']}
            persona = conn.execute('SELECT * FROM maintenance_persona WHERE run_id=?', (run_id,)).fetchone()
            if persona:
                report['persona'] = {**json.loads(persona['details_json']), 'status':persona['status'],
                                     'reason':persona['reason'], 'attempt_id':persona['attempt_id'],
                                     'due':json.loads(persona['due_json'])}
                if persona['status'] == 'skipped':
                    group = report['summary']['skipped']
                    group['reasons'][persona['reason']] = group['reasons'].get(persona['reason'], 0)+1
                    group['count'] += 1
            if model:
                from .consolidation import run_settings
                settings = run_settings(conn, run_id)
                report['goal_review'] = {'enabled':settings['enabled'] and settings['goal_review_enabled'],
                    'checked':progress.get('checks',{}).get('goals',0)}
                if not report['goal_review']['enabled']:
                    report['goal_review']['skip_reason'] = 'disabled'
            return report

    def _retention(self, run, *, stop=None):
        """Bounded, restart-safe deletes/redactions; no model calls or file I/O in transactions."""
        current = datetime.fromisoformat(run['created_at'])
        tasks = (
            ('model_calls', 'created_at', 90, None),
            ('recalls', 'created_at', 30, None),
            ('batch_attempts', 'finished_at', 90, ('raw_output', 'repair_output')),
            ('consolidation_calls', 'created_at', 90, ('raw_output',)),
        )
        for table, stamp, days, fields in tasks:
            cutoff = (current-timedelta(days=days)).isoformat()
            where = f'julianday({stamp})<julianday(?)'
            if fields:
                where += ' AND (' + ' OR '.join(f'{field} IS NOT NULL' for field in fields) + ')'
            while True:
                if stop and stop():
                    return False
                with self.store.read() as conn:
                    ids = [r[0] for r in conn.execute(f'SELECT id FROM {table} WHERE {where} LIMIT 128', (cutoff,))]
                if not ids:
                    break
                marks = ','.join('?' for _ in ids)
                action = (f'UPDATE {table} SET ' + ','.join(f'{field}=NULL' for field in fields)
                          if fields else f'DELETE FROM {table}')
                with self.store.write() as conn:
                    # Recalls cascade to recall_items and host_recalls in the same commit.
                    conn.execute(f'{action} WHERE id IN ({marks}) AND {where}', (*ids, cutoff))
        if stop and stop():
            return False
        pattern = re.compile(re.escape(self.store.path.name) + r'\.[0-9]{8}T[0-9]{6}Z\.bak')
        backups = sorted((path for path in self.store.path.parent.iterdir()
                          if pattern.fullmatch(path.name) and not path.is_symlink() and path.is_file()),
                         key=lambda path: path.name, reverse=True)
        for path in backups[3:]:
            if stop and stop():
                return False
            path.unlink(missing_ok=True)
        return True

    def _finish(self, run_id):
        report = self.report(run_id)
        with self.store.write() as conn:
            changed = conn.execute("UPDATE maintenance_runs SET state='completed',finished_at=?,summary_json=? WHERE id=? AND state='running'",
                                   (self.clock().isoformat(), dumps(report["summary"]), run_id)).rowcount
            if changed:
                operation(conn, "maintenance_completed", "maintenance", run_id,
                          {name: value["count"] for name, value in report["summary"].items()},
                          actor="system", stamp=self.clock().isoformat())
        cleanup(self.store, current=self.clock())
