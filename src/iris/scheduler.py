"""In-process scheduling backed by batches and missing memory vectors."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np

from .db import Store
from .learning import LearningEngine, PROMPT_VERSION
from .model_health import utc_now
from .maintenance import Maintenance
from .memory_ops import operation
from .models import ModelError
from .queue import FOCUS, form_batch, get_batch, should_learn


log = logging.getLogger("iris.scheduler")


class Scheduler:
    def __init__(self, store: Store, gateway, *, clock=utc_now, interval=0.5,
                 max_concurrent=None, config_loader=None):
        self.store, self.gateway, self.clock = store, gateway, clock
        self.health = getattr(gateway, "health", None)
        self.interval, self.config_loader = interval, config_loader
        self._loaded_configs = dict(gateway.configs)
        if max_concurrent is not None:
            self.set_concurrency(max_concurrent)
        self._pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="iris-learning")
        self._maintenance = ThreadPoolExecutor(max_workers=3, thread_name_prefix="iris-maintenance")
        self._active, self._probes = {}, {}
        self._vectors = None
        self.lifecycle = Maintenance(store, clock=clock)
        self._lifecycle_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="iris-lifecycle")
        self._lifecycle_job = None
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread = None
        self._tick_lock = threading.Lock()
        self._last_entry = None
        self.last_error = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def set_concurrency(self, value):
        if type(value) is not int or not 1 <= value <= 32:
            raise ValueError("learning concurrency must be 1..32")
        self.store.set_setting("learning_concurrency", value)

    def start(self):
        if self._thread:
            raise RuntimeError("scheduler already started")
        self.store.recover_inflight(current=self.clock)
        self._thread = threading.Thread(target=self._loop, name="iris-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started")

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as error:
                # Exception strings can contain user text or provider headers.
                self.last_error = type(error).__name__
                log.error("scheduler tick failed category=%s", self.last_error)
            self._wake.wait(self.interval)
            self._wake.clear()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join()
        # Finish accepted work before the shared Store and Gateway are closed.
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._maintenance.shutdown(wait=True, cancel_futures=True)
        self._lifecycle_pool.shutdown(wait=True, cancel_futures=True)
        log.info("scheduler stopped")

    def request_learning(self, entry_id, *, actor="host"):
        with self.store.write() as conn:
            if not conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone():
                raise KeyError(entry_id)
            pending, through = conn.execute("""SELECT COUNT(*),MAX(id) FROM messages
                WHERE entry_id=? AND learning_state IN ('pending','batched')""", (entry_id,)).fetchone()
            conn.execute("UPDATE entries SET learn_requested_through=? WHERE id=?", (through, entry_id))
            conn.execute("UPDATE batches SET next_retry_at=NULL WHERE entry_id=? AND state='waiting'", (entry_id,))
            operation(conn, "learn_requested", "entry", entry_id, {"pending_count": pending}, actor=actor)
        state = self.health.snapshot()["chat"] if self.health else {"state": "normal", "last_error": None}
        self.wake()
        return {"accepted": True, "pending_count": pending, "paused": state["state"] != "normal",
                "reason": state["last_error"] if state["state"] != "normal" else None}

    def request_maintenance(self):
        run_id = self.lifecycle.request(actor="admin")
        self.wake()
        return {"accepted": True, "run_id": run_id}

    def _schedule_lifecycle(self):
        if self._lifecycle_job is not None:
            if not self._lifecycle_job.done():
                return
            try:
                self._lifecycle_job.result()
            except Exception as error:
                log.error("lifecycle task interrupted category=%s", type(error).__name__)
            self._lifecycle_job = None
        run_id = self.lifecycle.pending()
        if run_id is None:
            due = self.lifecycle.due()
            if due:
                run_id = self.lifecycle.request(trigger=due[0], schedule_key=due[1])
        if run_id is not None:
            self._lifecycle_job = self._lifecycle_pool.submit(self.lifecycle.run, run_id, stop=self._stop.is_set)

    def _execute(self, batch_id):
        engine = LearningEngine(self.store, self.gateway, clock=self.clock)
        try:
            result = engine.run_batch(batch_id)
            log.info("batch finished id=%s state=%s", batch_id, result.get("state", "succeeded"))
        except Exception as error:
            log.error("batch failed id=%s category=%s", batch_id, type(error).__name__)
            batch = get_batch(self.store, batch_id)
            if batch.state == "running":
                with self.store.read() as conn:
                    number = conn.execute("SELECT COALESCE(MAX(number),0)+1 FROM batch_attempts WHERE batch_id=?", (batch_id,)).fetchone()[0]
                engine._fail(batch, {"number": number, "started_at": self.clock().isoformat(), "duration_ms": 0},
                             ModelError("internal", type(error).__name__))
        finally:
            self.wake()

    @staticmethod
    def _reap(futures):
        for key, future in list(futures.items()):
            if future.done():
                try:
                    future.result()
                except Exception as error:
                    log.error("background task failed category=%s", type(error).__name__)
                del futures[key]

    def tick(self):
        with self._tick_lock:
            if self._stop.is_set():
                return
            self._reap(self._active)
            self._reap(self._probes)
            self._schedule_lifecycle()
            if self.config_loader:
                try:
                    configs = self.config_loader()
                    for kind in ("chat", "embedding"):
                        if configs.get(kind) != self._loaded_configs.get(kind):
                            self.gateway.replace_config(kind, configs.get(kind))
                    self._loaded_configs = dict(configs)
                except (OSError, ValueError):
                    log.warning("model configuration could not be reloaded")
            if self.health:
                for kind in self.health.due_probes():
                    if kind not in self._probes:
                        self._probes[kind] = self._maintenance.submit(self.gateway.probe, kind)
                if self.health.allowed("embedding") and (self._vectors is None or self._vectors.done()):
                    if self._vectors:
                        try:
                            self._vectors.result()
                        except Exception as error:
                            log.error("vector backfill failed category=%s", type(error).__name__)
                    self._vectors = self._maintenance.submit(self.backfill_vectors)
                if not self.health.learning_allowed():
                    return
            capacity = int(self.store.setting("learning_concurrency", 2)) - len(self._active)
            if capacity <= 0:
                return
            with self.store.read() as conn:
                entries = [dict(r) for r in conn.execute("SELECT * FROM entries ORDER BY id")]
            # Round robin prevents one hot entry from monopolizing the only free slot.
            if self._last_entry:
                entries.sort(key=lambda row: (row["id"] <= self._last_entry, row["id"]))
            role_name = str(self.store.setting("role_name", "Iris"))
            for entry in entries:
                eid = entry["id"]
                if eid in self._active:
                    continue
                with self.store.read() as conn:
                    active = conn.execute("SELECT * FROM batches WHERE entry_id=? AND state IN ('waiting','running') ORDER BY id LIMIT 1", (eid,)).fetchone()
                    pending = conn.execute("SELECT id,content,received_at FROM messages WHERE entry_id=? AND learning_state='pending' ORDER BY id", (eid,)).fetchall()
                    latest = conn.execute("SELECT MAX(received_at) FROM messages WHERE entry_id=?", (eid,)).fetchone()[0]
                if active:
                    if active["state"] != "waiting" or (active["next_retry_at"] and datetime.fromisoformat(active["next_retry_at"]) > self.clock()):
                        continue
                    batch_id = active["id"]
                else:
                    if not pending:
                        continue
                    manual = entry["learn_requested_through"] is not None and pending[0]["id"] <= entry["learn_requested_through"]
                    focus = any(FOCUS.search(row["content"]) or role_name in row["content"] for row in pending)
                    if not should_learn(len(pending), min(datetime.fromisoformat(row["received_at"]) for row in pending),
                                        datetime.fromisoformat(latest), self.clock(), entry["pace"], focus=focus, manual=manual):
                        continue
                    batch = form_batch(self.store, eid, PROMPT_VERSION)
                    if batch is None:
                        continue
                    batch_id = batch.id
                self._active[eid] = self._pool.submit(self._execute, batch_id)
                self._last_entry = eid
                capacity -= 1
                if capacity <= 0:
                    break

    def backfill_vectors(self, limit=16):
        config = self.gateway.configs.get("embedding")
        if not config or not config.model or (self.health and not self.health.allowed("embedding")):
            return 0
        settings = self.store.setting("retrieval", {})
        dimensions = config.dimensions
        if dimensions is None and settings.get("embedding_model") == config.model:
            dimensions = settings.get("embedding_dimensions", 2048)
        vector_bytes = dimensions * 4 if dimensions is not None else None
        needs_vector = """(embedding IS NULL OR embedding_model IS NULL OR embedding_model!=?
                           OR (? IS NOT NULL AND length(embedding)!=?))"""
        with self.store.read() as conn:
            rows = conn.execute(f"""SELECT id,content,revision FROM memories WHERE lifecycle!='deleted'
                AND {needs_vector} ORDER BY id LIMIT ?""",
                (config.model, vector_bytes, vector_bytes, limit)).fetchall()
        count = 0
        for row in rows:
            if self._stop.is_set():
                break
            try:
                vector = np.asarray(self.gateway.embedding(row["content"], "memory_embedding"), dtype=np.float32)
            except ModelError:
                break
            if self.gateway.configs.get("embedding") != config:
                break
            with self.store.write() as conn:
                count += conn.execute(f"""UPDATE memories SET embedding=?,embedding_model=? WHERE id=? AND revision=?
                    AND lifecycle!='deleted' AND {needs_vector}""",
                    (vector.tobytes(), config.model, row["id"], row["revision"], config.model,
                     vector_bytes, vector_bytes)).rowcount
        return count
