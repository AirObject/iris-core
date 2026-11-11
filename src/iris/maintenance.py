"""Restartable daily maintenance with a separate, optional model phase."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import backup
from .db import dumps
from .memory_ops import adjust_retention, delete_memory, lifecycle_settings, message_references, missing_batch_targets, operation
from .model_health import utc_now
from .media import (DEFAULT_MEDIA_RETENTION_DAYS, GRACE_PERIOD, cleanup_upload_orphans,
                    delete_expired_file, delete_unreferenced_file, files_for_message,
                    mark_expired_file, mark_unreferenced, media_retention_days)
from .queue import reset_batch
from .runtime_journal import cleanup, maintenance_span, record

PHASES = ("decay", "expiry", "dependency", "messages", "retry", "consolidation", "persona", "goals", "media", "retention")
ABANDONED_RETRY_DAYS = [1, 3, 7]
# Bind the last scheduled day, then the number of scheduled retries. Needs b and s.
RETRY_PENDING = """b.state='abandoned' AND b.finished_at IS NOT NULL AND s.closed_reason IS NULL
    AND COALESCE(s.covered_day,0)<?
    AND (SELECT COUNT(*) FROM maintenance_batch_retries r WHERE r.batch_id=b.id)<?"""


def abandoned_retry_days(conn):
    row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='abandoned_retry_days'").fetchone()
    return json.loads(row[0]) if row else list(ABANDONED_RETRY_DAYS)


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
                config['media_retention_days'] = media_retention_days(self.store)
                config['abandoned_retry_days'] = abandoned_retry_days(conn)
                from .goals import auto_review_switches
                # G1/G2 switches are fixed for the run; an older run has none (off).
                config['goal_auto'] = auto_review_switches(conn)
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
                backup.run_scheduled_backup(self.store, current=self.clock())
                return
            scheduled = None
            try:
                if row['schedule_key']:
                    scheduled = datetime.fromisoformat(row['schedule_key']+'T'+json.loads(row['settings_json'])['maintenance_time']).replace(tzinfo=ZoneInfo(row['timezone']))
            except Exception:
                logging.getLogger('iris.runtime_journal').warning('runtime journal write failed')
            with maintenance_span(self.store, run_id, 'maintenance', clock=self.clock, stop=stop, scheduled_at=scheduled):
                self._run(run_id, stop=stop)
            with self.store.read() as conn:
                completed = conn.execute("SELECT state FROM maintenance_runs WHERE id=?", (run_id,)).fetchone()[0] == 'completed'
            if completed:
                backup.run_scheduled_backup(self.store, current=self.clock())

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
                if phase == "goals" and not self._goal_auto(run, stop=stop):
                    return
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

    # G line (full-auto stage): model judgments after the deterministic goal
    # items. Every model call is outside a transaction; each write is one short
    # transaction with its report item. A restarted run skips recorded
    # fingerprints, and failures stay unrecorded so a later pass retries them.
    def _goal_auto(self, run, *, stop=None):
        from .goals import auto_review_switches
        configured = json.loads(run["settings_json"]).get("goal_auto") or {}
        with self.store.read() as conn:
            live = auto_review_switches(conn)
        for section, step in (("duplicates", self._goal_duplicates), ("review", self._goal_auto_review)):
            if not configured.get(section):
                continue
            if not live[section]:
                with self.store.write() as conn:
                    self._goal_summary(conn, run, section, {**self._goal_counts(conn, run, section), "stopped": "disabled"})
                continue
            if not step(run, stop=stop):
                return False
        return True

    @staticmethod
    def _model_unavailable(error):
        return bool(error.paused or error.category in ("paused", "configuration", "account", "budget")
                    or error.reason in ("rate_limited", "unconfigured", "interrupted", "usage_limit"))

    def _goal_item(self, conn, run, item_key, object_id, outcome, reason, details):
        conn.execute("""INSERT INTO maintenance_items(run_id,phase,item_key,memory_id,object_id,outcome,reason,details_json,created_at)
            VALUES(?,'goals',?,NULL,?,?,?,?,?) ON CONFLICT(run_id,phase,item_key) DO UPDATE SET
            object_id=excluded.object_id,outcome=excluded.outcome,reason=excluded.reason,
            details_json=excluded.details_json,created_at=excluded.created_at""",
            (run["id"], item_key, object_id, outcome, reason, dumps(details), self.clock().isoformat()))

    @staticmethod
    def _goal_counts(conn, run, section):
        row = conn.execute("SELECT progress_json FROM maintenance_runs WHERE id=?", (run["id"],)).fetchone()
        return dict(json.loads(row[0]).get("goal_auto", {}).get(section, {}))

    @staticmethod
    def _goal_summary(conn, run, section, counts):
        # Counts accumulate across interrupted passes; the cursor marks the last
        # item whose outcome is already counted, in the same transaction.
        row = conn.execute("SELECT progress_json FROM maintenance_runs WHERE id=?", (run["id"],)).fetchone()
        progress = json.loads(row[0])
        progress.setdefault("goal_auto", {})[section] = counts
        conn.execute("UPDATE maintenance_runs SET progress_json=? WHERE id=? AND state='running'",
                     (dumps(progress), run["id"]))

    def _goal_step(self, run, section, counts, key, *, cursor, item=None, conn=None):
        """Count one finished item, advance the cursor and write both atomically."""
        if key:
            counts[key] = counts.get(key, 0) + 1
        counts["cursor"] = cursor
        if conn is not None:
            if item:
                self._goal_item(conn, run, *item)
            self._goal_summary(conn, run, section, counts)
            return
        with self.store.write() as write:
            if item:
                self._goal_item(write, run, *item)
            self._goal_summary(write, run, section, counts)

    def _goal_stop(self, run, section, counts, reason):
        counts["stopped"] = reason
        with self.store.write() as conn:
            self._goal_summary(conn, run, section, counts)

    @staticmethod
    def _failure_reason(error):
        # Safe categories only, never provider or exception text.
        return (error.reason or error.category) if hasattr(error, "category") else type(error).__name__

    def _goal_duplicates(self, run, *, stop=None):
        """G2: judge each possible-duplicate pair with the shared dedup material."""
        from .goals import (DUPLICATE_AUTO_MAX_CALLS, GoalConflict, apply_duplicate_judgment, duplicate_auto_admission,
                            duplicate_auto_judged, duplicate_auto_material, duplicate_auto_pairs, judge_duplicate_pair)
        from .models import ModelError
        with self.store.read() as conn:
            counts = self._goal_counts(conn, run, "duplicates") or {"pairs": 0, "calls": 0}
            pairs = duplicate_auto_pairs(conn, goal_through=run["goal_through"])
        counts.pop("stopped", None)
        done = tuple(counts.get("cursor") or (0, 0))
        for goal_a, goal_b in pairs:
            if (goal_a, goal_b) <= done:
                continue
            if stop and stop():
                return False
            cursor = [goal_a, goal_b]
            key = f"duplicate:{goal_a}:{goal_b}"
            failure = lambda error: (key, goal_a, "failed", self._failure_reason(error),  # noqa: E731
                                     {"kind": "duplicate", "goal_ids": [goal_a, goal_b]})
            counts["pairs"] = counts.get("pairs", 0) + 1
            try:
                with self.store.read() as conn:
                    material = duplicate_auto_material(conn, goal_a, goal_b)
                    judged = "skip" not in material and duplicate_auto_judged(conn, material)
            except KeyError:
                self._goal_step(run, "duplicates", counts, "pair_changed", cursor=cursor)
                continue
            except Exception as error:
                self._goal_step(run, "duplicates", counts, "failed", cursor=cursor, item=failure(error))
                continue
            if "skip" in material:
                if material["skip"] == "dedup_judge_disabled":
                    counts["pairs"] -= 1
                    self._goal_stop(run, "duplicates", counts, "dedup_judge_disabled")
                    break
                self._goal_step(run, "duplicates", counts, material["skip"], cursor=cursor)
                continue
            if judged:
                self._goal_step(run, "duplicates", counts, "already_judged", cursor=cursor)
                continue
            verdicts = {}
            if material["partner_in_material"] and not material.get("scope_blocked"):
                if counts.get("calls", 0) >= DUPLICATE_AUTO_MAX_CALLS:
                    self._goal_step(run, "duplicates", counts, "budget_exhausted", cursor=cursor)
                    continue
                try:
                    duplicate_auto_admission(self.gateway)
                except ModelError as error:
                    counts["pairs"] -= 1
                    self._goal_stop(run, "duplicates", counts, error.reason or error.category)
                    break
                counts["calls"] = counts.get("calls", 0) + 1
                try:
                    verdicts = judge_duplicate_pair(self.gateway, material)
                except ModelError as error:
                    self._goal_step(run, "duplicates", counts, "failed", cursor=cursor, item=failure(error))
                    if self._model_unavailable(error):
                        self._goal_stop(run, "duplicates", counts, error.reason or error.category)
                        break
                    continue
                except Exception as error:
                    self._goal_step(run, "duplicates", counts, "failed", cursor=cursor, item=failure(error))
                    continue
            try:
                with self.store.write() as conn:
                    result = apply_duplicate_judgment(conn, self.clock(), material=material, verdicts=verdicts, run_id=run["id"])
                    if result is None:
                        self._goal_step(run, "duplicates", counts, "already_judged", cursor=cursor, conn=conn)
                    else:
                        self._goal_step(run, "duplicates", counts, result["outcome"], cursor=cursor, conn=conn,
                                        item=(key, result["target_id"] or goal_a, "goal_duplicates_" + result["outcome"],
                                              result["reason"], {"kind": "duplicate", **result}))
            except (GoalConflict, KeyError):
                self._goal_step(run, "duplicates", counts, "revision_conflict", cursor=cursor)
            except Exception as error:
                self._goal_step(run, "duplicates", counts, "failed", cursor=cursor, item=failure(error))
        else:
            with self.store.write() as conn:
                self._goal_summary(conn, run, "duplicates", counts)
        return True

    def _goal_auto_review(self, run, *, stop=None):
        """G1: close learning-created goals only on explicit, code-checked evidence."""
        from .goals import (AUTO_REVIEW_MAX_CALLS, GoalConflict, apply_goal_review, auto_review_admission,
                            auto_review_goal_ids, auto_review_judged, auto_review_material, judge_goal_review)
        from .models import ModelError
        as_of = datetime.fromisoformat(run["created_at"])
        with self.store.read() as conn:
            counts = self._goal_counts(conn, run, "review") or {"goals": 0, "calls": 0}
            goals = auto_review_goal_ids(conn, goal_through=run["goal_through"])
        counts.pop("stopped", None)
        done = counts.get("cursor") or 0
        for goal_id, _ in goals:
            if goal_id <= done:
                continue
            if stop and stop():
                return False
            key = f"auto_review:{goal_id}"
            counts["goals"] = counts.get("goals", 0) + 1
            try:
                with self.store.read() as conn:
                    material = auto_review_material(conn, goal_id, current=as_of, message_through=run["message_through"])
                    judged = material is not None and auto_review_judged(conn, material)
            except KeyError:
                self._goal_step(run, "review", counts, "goal_missing", cursor=goal_id)
                continue
            except Exception as error:
                self._goal_step(run, "review", counts, "failed", cursor=goal_id,
                                item=(key, goal_id, "failed", self._failure_reason(error), {"kind": "auto_review"}))
                continue
            if material is None:
                # No candidate message: never call the model.
                self._goal_step(run, "review", counts, "no_candidates", cursor=goal_id)
                continue
            if judged:
                self._goal_step(run, "review", counts, "already_judged", cursor=goal_id)
                continue
            if counts.get("calls", 0) >= AUTO_REVIEW_MAX_CALLS:
                self._goal_step(run, "review", counts, "budget_exhausted", cursor=goal_id)
                continue
            failure = lambda error: (key, goal_id, "failed", self._failure_reason(error),  # noqa: E731
                                     {"kind": "auto_review", "goal_revision": material["revision"]})
            try:
                # Pause and the daily token limit stop the pass before any request;
                # nothing is recorded for this goal, so a later run judges it.
                auto_review_admission(self.gateway)
            except ModelError as error:
                counts["goals"] -= 1
                self._goal_stop(run, "review", counts, error.reason or error.category)
                break
            counts["calls"] = counts.get("calls", 0) + 1
            try:
                result = judge_goal_review(self.gateway, material)
            except ModelError as error:
                self._goal_step(run, "review", counts, "failed", cursor=goal_id, item=failure(error))
                if self._model_unavailable(error):
                    self._goal_stop(run, "review", counts, error.reason or error.category)
                    break
                continue
            except Exception as error:
                self._goal_step(run, "review", counts, "failed", cursor=goal_id, item=failure(error))
                continue
            try:
                with self.store.write() as conn:
                    applied = apply_goal_review(conn, self.clock(), material=material, result=result, run_id=run["id"])
                    if applied is None:
                        self._goal_step(run, "review", counts, "already_judged", cursor=goal_id, conn=conn)
                    else:
                        outcome = ("goals_auto_" + applied["decision"] if applied["outcome"] == "closed"
                                   else "goal_review_kept")
                        self._goal_step(run, "review", counts, applied["outcome"], cursor=goal_id, conn=conn,
                                        item=(key, goal_id, outcome, applied["reason"], {"kind": "auto_review", **applied}))
            except (GoalConflict, KeyError):
                self._goal_step(run, "review", counts, "revision_conflict", cursor=goal_id)
            except Exception as error:
                self._goal_step(run, "review", counts, "failed", cursor=goal_id, item=failure(error))
        else:
            with self.store.write() as conn:
                self._goal_summary(conn, run, "review", counts)
        return True

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
                    FROM media_files WHERE id>? AND id<=? AND (
                    (unreferenced_at IS NOT NULL AND julianday(unreferenced_at)<=julianday(?))
                    OR bytes_purge_pending=1 OR (bytes_purged_at IS NULL AND julianday(created_at)<julianday(?)))
                    ORDER BY id LIMIT 128""",
                    (run['cursor_id'], run['media_through'], (current-GRACE_PERIOD).isoformat(),
                     (current-timedelta(days=config.get('media_retention_days', DEFAULT_MEDIA_RETENTION_DAYS))).isoformat()))]
        if phase == "goals":
            from .consolidation import run_settings
            with self.store.read() as conn:
                settings = run_settings(conn, run['id'])
                basis_review = settings['enabled'] and settings['goal_review_enabled']
                return [dict(r) for r in conn.execute("""SELECT id,id AS cursor_id,revision,CAST(id AS TEXT) AS item_key
                    FROM goals WHERE id>? AND id<=? AND state='open' AND merged_into IS NULL
                    AND (? OR (origin='internal' AND kind='question')) ORDER BY id LIMIT 100""",
                    (run['cursor_id'],run['goal_through'],basis_review))]
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
            days = self._retry_days(config)
            # Whether a slot has arrived is decided per item, under the write lock.
            select = f"""SELECT b.id,b.id AS cursor_id,CAST(b.id AS TEXT) AS item_key FROM batches b
                LEFT JOIN batch_retry_schedules s ON s.batch_id=b.id
                WHERE b.id>? AND b.id<=? AND {RETRY_PENDING}"""
            params = [run["cursor_id"], run["batch_through"], days[-1], len(days)]
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
            if phase == 'media':
                # Commit intent separately from unlink/progress. A restart can
                # complete a pending deletion even after unlink succeeded.
                with self.store.write() as conn:
                    if self._progress(conn, run, candidate) is None:
                        return
                    config = json.loads(run['settings_json'])
                    mark_expired_file(self.store, conn, candidate['id'],
                        as_of=datetime.fromisoformat(run['created_at']), current=self.clock(),
                        days=config.get('media_retention_days', DEFAULT_MEDIA_RETENTION_DAYS))
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
            deleted = delete_unreferenced_file(self.store, conn, mid, executed_at)
            if not deleted and delete_expired_file(self.store, conn, mid, executed_at):
                return 'media_purged', None, {}
            return ("media_deleted", None, {}) if deleted else ("skipped", "media_referenced_or_not_due", {})
        if phase == "goals":
            from .goals import expire_internal_question, review_goal_basis, GoalConflict
            from .consolidation import run_settings
            try:
                if expire_internal_question(conn, executed_at, goal_id=mid, expected_revision=candidate['revision'],
                                            as_of=current, zone=ZoneInfo(run['timezone'])):
                    return 'questions_expired', 'question_expired', {}
                settings = run_settings(conn, run['id'])
                if not settings['enabled'] or not settings['goal_review_enabled']:
                    return 'checked', None, {}
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
                    result = adjust_retention(self.store, mid, current=executed_at, settings=config, _conn=conn)
                    if result is not None and result["lifecycle"] == "active":
                        return "restored", None, {"before": memory["retention"], "after": memory["retention"]}
                    # A superseded memory stays forgotten until an administrator restores it.
                    return "skipped", "superseded", {}
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
                # Only this run's retry phase is protected: later slots do not extend retention.
                days = self._retry_days(config)
                for batch in conn.execute(f"""SELECT COALESCE(s.abandoned_at,b.finished_at) AS abandoned_at,
                        COALESCE(s.covered_day,0) AS covered_day
                        FROM batches b JOIN json_each(b.target_ids) target
                        LEFT JOIN batch_retry_schedules s ON s.batch_id=b.id
                        WHERE b.id<=? AND target.value=? AND {RETRY_PENDING}""",
                        (run['batch_through'], mid, days[-1], len(days))).fetchall():
                    if self._reached_slots(batch, days, run, current):
                        return 'skipped', 'batch_retry_pending', {}
            references = message_references(conn, mid)
            if references:
                return "skipped", "referenced", {"references": references}
            media_files = files_for_message(conn, mid)
            conn.execute("DELETE FROM messages WHERE id=?", (mid,))
            mark_unreferenced(conn, media_files, executed_at)
            return "messages_deleted", None, {}
        return self._retry_abandoned(conn, run, config, current, mid)

    @staticmethod
    def _retry_days(config):
        # A run accepted before this setting existed has no key in its snapshot.
        return config.get("abandoned_retry_days") or ABANDONED_RETRY_DAYS

    @staticmethod
    def _reached_slots(batch, days, run, current):
        zone = ZoneInfo(run["timezone"])
        elapsed = (current.astimezone(zone).date()-datetime.fromisoformat(batch["abandoned_at"]).astimezone(zone).date()).days
        return [day for day in days if batch["covered_day"] < day <= elapsed]

    def _retry_abandoned(self, conn, run, config, current, mid):
        days = self._retry_days(config)
        today = current.astimezone(ZoneInfo(run["timezone"])).date()
        batch = conn.execute("""SELECT b.state,COALESCE(s.abandoned_at,b.finished_at) AS abandoned_at,
            COALESCE(s.covered_day,0) AS covered_day,
            (SELECT COUNT(*) FROM maintenance_batch_retries r WHERE r.batch_id=b.id) AS retries
            FROM batches b LEFT JOIN batch_retry_schedules s ON s.batch_id=b.id WHERE b.id=?""", (mid,)).fetchone()
        if batch is None or batch["state"] != "abandoned":
            return "skipped", "batch_state_changed", {}
        reached = self._reached_slots(batch, days, run, current)
        if not reached or batch["retries"] >= len(days):
            return "checked", None, {}
        if conn.execute("SELECT 1 FROM maintenance_batch_retries WHERE batch_id=? AND local_day=?",
                        (mid, today.isoformat())).fetchone():
            return "skipped", "already_retried", {}
        if missing_batch_targets(conn, mid):
            # IDs are never reused, so report this once instead of on every later slot.
            conn.execute("""INSERT INTO batch_retry_schedules(batch_id,abandoned_at,closed_reason) VALUES(?,?,?)
                ON CONFLICT(batch_id) DO UPDATE SET closed_reason=excluded.closed_reason""",
                (mid, batch["abandoned_at"], "target_messages_cleared"))
            return "skipped", "target_messages_cleared", {}
        reset_batch(self.store, mid, _conn=conn)
        number = batch["retries"]+1
        conn.execute("""INSERT INTO maintenance_batch_retries(batch_id,number,run_id,local_day,created_at)
            VALUES(?,?,?,?,?)""", (mid, number, run["id"], today.isoformat(), self.clock().isoformat()))
        # Slots missed while the service was down collapse into this one retry.
        conn.execute("UPDATE batch_retry_schedules SET covered_day=? WHERE batch_id=?", (reached[-1], mid))
        return "batches_retried", None, {"retry_number": number, "schedule_day": reached[-1], "missed_days": reached[:-1]}

    @staticmethod
    def _summary(items, progress):
        result = {name: {"count": 0, "memory_ids": [], "object_ids": []} for name in (
            "decayed", "checked", "forgotten", "restored", "deleted", "dependencies_weakened",
            "messages_deleted", "batches_retried", "questions_expired", "skipped", "failed")}
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
            expired = report['summary']['questions_expired']
            report['question_expiry'] = {'count':expired['count'], 'goal_ids':expired['object_ids']}
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
            configured = report['settings'].get('goal_auto') or {}
            for section, name in (('review', 'goal_auto_review'), ('duplicates', 'goal_duplicate_review')):
                report[name] = {'enabled': bool(configured.get(section)),
                                **{k: v for k, v in progress.get('goal_auto', {}).get(section, {}).items() if k != 'cursor'}}
                if not report[name]['enabled']:
                    report[name]['skip_reason'] = 'disabled'
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
