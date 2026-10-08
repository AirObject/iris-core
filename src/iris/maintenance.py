"""Restartable deterministic lifecycle maintenance; no model or network calls."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import dumps
from .memory_ops import adjust_retention, delete_memory, lifecycle_settings, message_references, operation
from .model_health import utc_now
from .queue import reset_batch

PHASES = ("decay", "expiry", "dependency", "messages", "retry")


class Maintenance:
    def __init__(self, store, *, clock=utc_now):
        self.store, self.clock = store, clock
        self.started_at = self._last_poll = clock()
        with store.read() as conn:
            completed = conn.execute("""SELECT finished_at FROM maintenance_runs WHERE state='completed'
                ORDER BY julianday(finished_at) DESC LIMIT 1""").fetchone()
        self._startup_catchup_done = bool(completed and self.started_at-datetime.fromisoformat(completed[0]) <= timedelta(hours=24))
        self._lock = threading.Lock()

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
                return "catchup", None
        return None

    def pending(self):
        with self.store.read() as conn:
            row = conn.execute("SELECT id FROM maintenance_runs WHERE state='running'").fetchone()
            return row[0] if row else None

    def request(self, *, trigger="manual", schedule_key=None, actor=None):
        with self.store.write() as conn:
            row = conn.execute("SELECT id FROM maintenance_runs WHERE state='running'").fetchone()
            if row is None and schedule_key:
                row = conn.execute("SELECT id FROM maintenance_runs WHERE schedule_key=?", (schedule_key,)).fetchone()
            if row:
                rid = row[0]
            else:
                config = lifecycle_settings(conn)
                zone_row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='timezone'").fetchone()
                bounds = [conn.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()[0]
                          for table in ("memories", "messages", "batches")]
                rid = conn.execute("""INSERT INTO maintenance_runs(trigger,schedule_key,settings_json,timezone,
                    memory_through,message_through,batch_through,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (trigger, schedule_key, dumps(config), json.loads(zone_row[0]) if zone_row else "Asia/Shanghai",
                     *bounds, self.clock().isoformat())).lastrowid
            if actor:
                operation(conn, "maintenance_requested", "maintenance", rid, actor=actor, stamp=self.clock().isoformat())
        if trigger in ("manual", "scheduled", "catchup"):
            self._startup_catchup_done = True
        return rid

    def run(self, run_id, *, stop=None):
        # A single service worker owns normal execution. The journal additionally
        # guards each item in its write transaction, even for duplicate invocations.
        with self._lock:
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
                candidates = self._candidates(run, phase)
                if not candidates:
                    with self.store.write() as conn:
                        conn.execute("UPDATE maintenance_runs SET phase=phase+1 WHERE id=? AND phase=?", (run_id, run["phase"]))
                    continue
                for candidate in candidates:
                    if stop and stop():
                        return
                    self._process_item(run, phase, candidate)

    def _candidates(self, run, phase):
        config = json.loads(run["settings_json"])
        current = datetime.fromisoformat(run["created_at"])
        params = []
        if phase in ("decay", "expiry"):
            where = "m.id<=? AND m.lifecycle!='deleted'"
            params = [run["memory_through"]]
            if phase == "expiry":
                if not config["auto_delete_enabled"]:
                    return []
                where += " AND m.lifecycle='forgotten' AND julianday(m.forgotten_at)<=julianday(?)"
                params.append((current-timedelta(days=config["auto_delete_days"])).isoformat())
            select = "SELECT m.id,m.revision,CAST(m.id AS TEXT) AS item_key FROM memories m WHERE " + where
        elif phase == "dependency":
            select = """SELECT d.memory_id AS id,m.revision,d.source_memory_id,
                CAST(d.memory_id AS TEXT)||':'||CAST(d.source_memory_id AS TEXT) AS item_key
                FROM memory_dependency_losses d JOIN memories m ON m.id=d.memory_id
                WHERE d.applied_at IS NULL AND d.memory_id<=?"""
            params = [run["memory_through"]]
        elif phase == "messages":
            select = """SELECT id,CAST(id AS TEXT) AS item_key FROM messages
                WHERE id<=? AND learning_state IN ('learned','abandoned') AND julianday(received_at)<julianday(?)"""
            params = [run["message_through"], (current-timedelta(days=config["message_retention_days"])).isoformat()]
        else:
            if not config["abandoned_retry_enabled"]:
                return []
            date = current.astimezone(ZoneInfo(run["timezone"])).replace(hour=0, minute=0, second=0, microsecond=0)
            select = """SELECT id,CAST(id AS TEXT) AS item_key FROM batches b
                WHERE id<=? AND state='abandoned' AND julianday(finished_at)>=julianday(?) AND julianday(finished_at)<julianday(?)
                AND NOT EXISTS(SELECT 1 FROM maintenance_batch_retries r WHERE r.batch_id=b.id)"""
            params = [run["batch_through"], (date-timedelta(days=1)).isoformat(), date.isoformat()]
        with self.store.read() as conn:
            return [dict(r) for r in conn.execute(f"""SELECT c.* FROM ({select}) c WHERE NOT EXISTS(
                SELECT 1 FROM maintenance_items i WHERE i.run_id=? AND i.phase=? AND i.item_key=c.item_key)
                ORDER BY c.id,c.item_key LIMIT 128""", [*params, run["id"], phase])]

    def _journal(self, conn, run, phase, candidate, outcome, reason, details):
        conn.execute("""INSERT OR IGNORE INTO maintenance_items
            (run_id,phase,item_key,memory_id,object_id,outcome,reason,details_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (run["id"], phase, candidate["item_key"],
            candidate["id"] if phase in ("decay", "expiry", "dependency") else None,
            candidate["id"], outcome, reason, dumps(details), self.clock().isoformat()))

    def _process_item(self, run, phase, candidate):
        try:
            with self.store.write() as conn:
                if conn.execute("SELECT 1 FROM maintenance_items WHERE run_id=? AND phase=? AND item_key=?",
                                (run["id"], phase, candidate["item_key"])).fetchone():
                    return
                outcome, reason, details = self._apply(conn, run, phase, candidate)
                self._journal(conn, run, phase, candidate, outcome, reason, details)
        except Exception as exc:
            # Transaction rolled back; only a safe category is persisted, never
            # arbitrary exception text (which might contain private model output).
            with self.store.write() as conn:
                self._journal(conn, run, phase, candidate, "failed", type(exc).__name__, {})

    def _apply(self, conn, run, phase, candidate):
        config = json.loads(run["settings_json"])
        current = datetime.fromisoformat(run["created_at"])
        # Selection cutoffs belong to the persisted run, but a new lifecycle
        # transition starts its retention period at the actual execution time.
        executed_at = self.clock()
        mid = candidate["id"]
        if phase in ("decay", "expiry", "dependency"):
            memory = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            if memory is None or memory["lifecycle"] == "deleted":
                return "skipped", "deleted", {}
            if memory["revision"] != candidate["revision"]:
                return "skipped", "revision_conflict", {}
        if phase == "decay":
            amount = 0
            if not memory["pinned"]:
                if memory["importance"] < 40:
                    amount = config["decay_amount"]
                elif memory["importance"] < 70:
                    visits = (memory["decay_visits"]+1) % 3
                    conn.execute("UPDATE memories SET decay_visits=? WHERE id=?", (visits, mid))
                    if visits == 0:
                        amount = config["decay_amount"]
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
            return "dependencies_weakened", None, details
        if phase == "messages":
            message = conn.execute("SELECT learning_state,received_at FROM messages WHERE id=?", (mid,)).fetchone()
            if not message or message["learning_state"] not in ("learned", "abandoned") or current-datetime.fromisoformat(message["received_at"]) <= timedelta(days=config["message_retention_days"]):
                return "skipped", "no_longer_eligible", {}
            references = message_references(conn, mid)
            if references:
                return "skipped", "referenced", {"references": references}
            conn.execute("DELETE FROM messages WHERE id=?", (mid,))
            return "messages_deleted", None, {}
        batch = conn.execute("SELECT state,finished_at FROM batches WHERE id=?", (mid,)).fetchone()
        if batch is None or batch["state"] != "abandoned":
            return "skipped", "batch_state_changed", {}
        if conn.execute("SELECT 1 FROM maintenance_batch_retries WHERE batch_id=?", (mid,)).fetchone():
            return "skipped", "already_retried", {}
        yesterday = current.astimezone(ZoneInfo(run["timezone"])).date()-timedelta(days=1)
        if not batch["finished_at"] or datetime.fromisoformat(batch["finished_at"]).astimezone(ZoneInfo(run["timezone"])).date() != yesterday:
            return "skipped", "batch_date_changed", {}
        reset_batch(self.store, mid, _conn=conn)
        conn.execute("INSERT INTO maintenance_batch_retries VALUES(?,?,?)", (mid, run["id"], self.clock().isoformat()))
        return "batches_retried", None, {}

    @staticmethod
    def _summary(items):
        result = {name: {"count": 0, "memory_ids": [], "object_ids": []} for name in (
            "decayed", "checked", "forgotten", "restored", "deleted", "dependencies_weakened",
            "messages_deleted", "batches_retried", "skipped", "failed")}
        for item in items:
            names = [item["outcome"]]
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
        return result

    def report(self, run_id):
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM maintenance_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            report = dict(row)
            report["settings"] = json.loads(report.pop("settings_json"))
            report.pop("summary_json")
            items = []
            for item in conn.execute("SELECT * FROM maintenance_items WHERE run_id=? ORDER BY rowid", (run_id,)):
                item = dict(item)
                item["details"] = json.loads(item.pop("details_json"))
                items.append(item)
            report["items"] = items
            report["summary"] = self._summary(items)
            return report

    def _finish(self, run_id):
        report = self.report(run_id)
        with self.store.write() as conn:
            changed = conn.execute("UPDATE maintenance_runs SET state='completed',finished_at=?,summary_json=? WHERE id=? AND state='running'",
                                   (self.clock().isoformat(), dumps(report["summary"]), run_id)).rowcount
            if changed:
                operation(conn, "maintenance_completed", "maintenance", run_id,
                          {name: value["count"] for name, value in report["summary"].items()},
                          actor="system", stamp=self.clock().isoformat())
