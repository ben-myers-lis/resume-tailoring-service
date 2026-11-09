"""Background queue: N worker threads take QUEUED runs from the database and process them.

Runs are stored in the database, so the queue survives restarts (runs left RUNNING by a crash
are re-queued on startup). Increase RTS_WORKERS to process more resumes at once; with Claude,
the real limit is your Anthropic account's rate limits.
"""
from __future__ import annotations

import logging
import threading

from .config import Settings
from .db import DB
from .pipeline import process_run

log = logging.getLogger("rts.worker")


class WorkerPool:
    def __init__(self, db: DB, settings: Settings, poll_seconds: float = 0.5):
        self.db, self.settings, self.poll = db, settings, poll_seconds
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        n = self.db.requeue_stale()
        if n:
            log.info("re-queued %d interrupted run(s)", n)
        for i in range(self.settings.workers):
            t = threading.Thread(target=self._loop, name=f"rts-worker-{i}", daemon=True)
            t.start()
            self.threads.append(t)
        log.info("started %d worker(s), provider=%s", self.settings.workers, self.settings.ai_provider)

    def stop(self, timeout: float = 10) -> None:
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            run = self.db.claim_next_run()
            if run is None:
                self.stop_event.wait(self.poll)
                continue
            try:
                process_run(self.db, self.settings, run)
            except Exception:  # never let one run kill the worker
                log.exception("unexpected error in run %s", run["id"])
                self.db.finish_run(run["id"], status="FAILED", error="Unexpected worker error")
