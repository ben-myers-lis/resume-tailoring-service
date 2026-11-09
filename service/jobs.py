"""How queued runs get processed.

Every run is a row in the database first (status QUEUED), so the database is always the source of
truth. A dispatcher only decides *who* picks the run up:

  threads  Worker threads inside the web process poll the database (local, Docker, one server).
  vercel   Each new run is sent to Vercel Queues; Vercel calls the subscriber in rts_worker.py,
           one function invocation per run. Nothing has to stay running between jobs.

Safety nets for the queue (a message can be lost if sending fails, or a function can be stopped
by its time limit mid-run): `sweep()` re-sends runs that have waited too long and retries runs
that were abandoned while RUNNING, up to RTS_MAX_ATTEMPTS, then marks them FAILED. It runs from
the cron endpoint and, at most once a minute, whenever someone opens the dashboard or run list.
Claiming a run is atomic (db.claim_run), so duplicate deliveries never process a run twice.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import Settings, db_target, get_settings
from .db import DB
from .pipeline import process_run

log = logging.getLogger("rts.jobs")
TOPIC = "rts-runs"
QUEUED_GRACE_SECONDS = 120          # a queued run untouched for this long is re-sent


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


class Dispatcher:
    name = "base"

    def __init__(self, db: DB, settings: Settings):
        self.db, self.settings = db, settings
        self._last_sweep = 0.0
        self._sweep_lock = threading.Lock()

    def start(self) -> None: ...
    def stop(self) -> None: ...

    def enqueue(self, run_ids: list[str]) -> None:
        """Called right after runs are created."""

    def _send(self, rid: str, attempts: int) -> None:
        """Hand one run to whoever processes runs."""

    def sweep(self) -> dict:
        """Re-send waiting runs and retry (or fail) abandoned ones. Safe to call any time."""
        s = self.settings
        stats = {"resent": 0, "retried": 0, "failed": 0}
        running_before = _iso_ago(s.run_timeout)
        for r in self.db.runs_to_dispatch(_iso_ago(QUEUED_GRACE_SECONDS), running_before):
            if r["status"] == "RUNNING":
                if r["attempts"] >= s.max_attempts:
                    if self.db.requeue_run(r["id"], running_before):       # take it back first (atomic)
                        self.db.finish_run(r["id"], status="FAILED",
                                           error=f"Stopped after {r['attempts']} attempt(s): it took longer than "
                                                 f"{s.run_timeout // 60} minutes.")
                        stats["failed"] += 1
                    continue
                if not self.db.requeue_run(r["id"], running_before):
                    continue
                stats["retried"] += 1
            else:
                stats["resent"] += 1
            self._send(r["id"], r["attempts"])
        if any(stats.values()):
            log.info("sweep: %s", stats)
        return stats

    def maybe_sweep(self, every_seconds: float = 60) -> None:
        """Cheap, rate-limited sweep for request handlers."""
        if time.time() - self._last_sweep < every_seconds or not self._sweep_lock.acquire(blocking=False):
            return
        try:
            self._last_sweep = time.time()
            self.sweep()
        except Exception:
            log.exception("sweep failed")
        finally:
            self._sweep_lock.release()


class ThreadDispatcher(Dispatcher):
    """Worker threads in this process. Use with exactly one running instance."""
    name = "threads"

    def __init__(self, db: DB, settings: Settings):
        super().__init__(db, settings)
        from .worker import WorkerPool
        self.pool = WorkerPool(db, settings)

    def start(self) -> None:
        self.pool.start()

    def stop(self) -> None:
        self.pool.stop()

    def maybe_sweep(self, every_seconds: float = 60) -> None:
        """Workers already poll for QUEUED runs, and interrupted runs are re-queued on start-up."""


class VercelDispatcher(Dispatcher):
    name = "vercel"

    def __init__(self, db: DB, settings: Settings):
        super().__init__(db, settings)
        self._client = None

    def _queue(self):
        if self._client is None:
            from vercel.queue.sync import QueueClient   # pip install vercel-queue
            self._client = QueueClient()
        return self._client

    def enqueue(self, run_ids: list[str]) -> None:
        for rid in run_ids:
            self._send(rid, 0)

    def _send(self, rid: str, attempts: int) -> None:
        # The key makes a re-send of the same attempt a no-op if the first send did get through.
        try:
            self._queue().send(TOPIC, {"run_id": rid}, idempotency_key=f"{rid}-{attempts}")
        except Exception as e:
            if type(e).__name__ == "DuplicateIdempotencyKeyError":
                return
            # The run stays QUEUED in the database; the next sweep sends it again.
            log.warning("could not send run %s to Vercel Queues: %s: %s", rid, type(e).__name__, e)


def make_dispatcher(db: DB, settings: Settings) -> Dispatcher:
    if settings.queue == "vercel":
        return VercelDispatcher(db, settings)
    if settings.queue != "threads":
        raise ValueError("RTS_QUEUE must be 'threads' or 'vercel'")
    return ThreadDispatcher(db, settings)


# ---------- queue consumer (called by rts_worker.py on Vercel) ----------
_shared: dict = {}
_shared_lock = threading.Lock()


def _db_and_settings() -> tuple[DB, Settings]:
    with _shared_lock:
        if "db" not in _shared:
            s = get_settings()
            _shared["settings"], _shared["db"] = s, DB(db_target(s), s.db_schema)
        return _shared["db"], _shared["settings"]


def process_message(payload: dict, db: DB | None = None, settings: Settings | None = None) -> str:
    """Process the run named in a queue message. Returns what happened (for logs and tests).

    Never raises for problems with the run itself (those are recorded on the run), so the queue
    doesn't redeliver; database outages do raise, so the queue retries later.
    """
    if db is None or settings is None:
        db, settings = _db_and_settings()
    rid = (payload or {}).get("run_id")
    if not rid:
        return "ignored"
    run = db.claim_run(rid, stale_before=_iso_ago(settings.run_timeout))
    if run is None:
        return "not-claimed"          # done already, deleted, or being processed by another invocation
    if run["attempts"] > settings.max_attempts:
        db.finish_run(rid, status="FAILED", error=f"Stopped after {settings.max_attempts} attempts.")
        return "failed"
    try:
        process_run(db, settings, run)
    except Exception:                 # process_run records its own failures; this is a last resort
        log.exception("unexpected error in run %s", rid)
        db.finish_run(rid, status="FAILED", error="Unexpected worker error")
    return "processed"
