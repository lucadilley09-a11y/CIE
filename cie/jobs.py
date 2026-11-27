"""Bounded local job queue with progress, cancellation, timeout, and crash recovery."""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from .config import redact_secrets, utc_now
from .storage import get_storage


class JobManager:
    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cie-job")
        self._lock = threading.RLock()
        self._futures: dict[str, Future[Any]] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._mark_stale_running_interrupted()

    def _mark_stale_running_interrupted(self) -> None:
        storage = get_storage()
        storage.execute(
            "UPDATE jobs SET status = 'interrupted', message = 'Interrupted by restart', updated_at = ? WHERE status IN ('queued', 'running', 'cancelling')",
            (utc_now(),),
        )

    def submit(
        self,
        capability_id: str,
        fn: Callable[[Callable[[float, str], None], threading.Event], Any],
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        now = utc_now()
        cancel_event = threading.Event()
        timed_out = threading.Event()
        timeout = max(0.1, min(float(timeout_sec), 3600.0))
        storage = get_storage()
        storage.execute(
            "INSERT INTO jobs(job_id, capability_id, status, progress, message, created_at, updated_at) VALUES (?, ?, 'queued', 0.0, 'Queued', ?, ?)",
            (job_id, capability_id, now, now),
        )

        def update_progress(pct: float, msg: str = "") -> None:
            # A late worker must not overwrite a terminal timeout/cancel state.
            storage.execute(
                "UPDATE jobs SET progress = ?, message = ?, updated_at = ? WHERE job_id = ? AND status = 'running'",
                (max(0.0, min(1.0, float(pct))), str(msg)[:1000], utc_now(), job_id),
            )

        def runner() -> Any:
            if cancel_event.is_set():
                storage.execute(
                    "UPDATE jobs SET status = 'cancelled', message = 'Cancelled before start', updated_at = ? WHERE job_id = ? AND status = 'queued'",
                    (utc_now(), job_id),
                )
                return None
            storage.execute(
                "UPDATE jobs SET status = 'running', message = 'Running', updated_at = ? WHERE job_id = ? AND status = 'queued'",
                (utc_now(), job_id),
            )

            def mark_timeout() -> None:
                timed_out.set()
                cancel_event.set()
                storage.execute(
                    "UPDATE jobs SET status = 'timed_out', message = 'Timed out; cancellation requested (worker must cooperate)', updated_at = ? WHERE job_id = ? AND status = 'running'",
                    (utc_now(), job_id),
                )

            timer = threading.Timer(timeout, mark_timeout)
            timer.daemon = True
            timer.start()
            try:
                res = fn(update_progress, cancel_event)
                if timed_out.is_set():
                    return None
                if cancel_event.is_set():
                    storage.execute(
                        "UPDATE jobs SET status = 'cancelled', message = 'Cancelled', updated_at = ? WHERE job_id = ? AND status IN ('running', 'cancelling')",
                        (utc_now(), job_id),
                    )
                    return None
                storage.execute(
                    "UPDATE jobs SET status = 'completed', progress = 1.0, message = 'Completed', result_json = ?, updated_at = ? WHERE job_id = ? AND status = 'running'",
                    (json.dumps(res, default=str), utc_now(), job_id),
                )
                return res
            except Exception as exc:
                if not timed_out.is_set():
                    storage.execute(
                        "UPDATE jobs SET status = 'failed', error = ?, message = 'Failed', updated_at = ? WHERE job_id = ? AND status = 'running'",
                        (redact_secrets(str(exc))[:2000], utc_now(), job_id),
                    )
                return None
            finally:
                timer.cancel()

        with self._lock:
            self._cancel_flags[job_id] = cancel_event
            fut = self._executor.submit(runner)
            self._futures[job_id] = fut
            def _cleanup(done: Future[Any]) -> None:
                with self._lock:
                    if self._futures.get(job_id) is done:
                        self._futures.pop(job_id, None)
                    self._cancel_flags.pop(job_id, None)
            fut.add_done_callback(_cleanup)
        return {"job_id": job_id, "capability_id": capability_id, "status": "queued", "timeout_sec": timeout}

    def status(self, job_id: str) -> dict[str, Any] | None:
        rows = get_storage().query("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        if not rows:
            return None
        row = rows[0]
        if row.get("result_json"):
            try:
                row["result"] = json.loads(row["result_json"])
            except Exception:
                row["result"] = None
        row.pop("result_json", None)
        return row

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            ev = self._cancel_flags.get(job_id)
            if ev:
                ev.set()
            fut = self._futures.get(job_id)
            if fut and fut.cancel():
                get_storage().execute(
                    "UPDATE jobs SET status = 'cancelled', message = 'Cancelled in queue', updated_at = ? WHERE job_id = ? AND status = 'queued'",
                    (utc_now(), job_id),
                )
            elif fut:
                get_storage().execute(
                    "UPDATE jobs SET status = 'cancelling', message = 'Cancellation requested; worker must cooperate', updated_at = ? WHERE job_id = ? AND status = 'running'",
                    (utc_now(), job_id),
                )
        return self.status(job_id) or {"job_id": job_id, "status": "not_found"}

    def list_jobs(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = get_storage().query(
            "SELECT job_id, capability_id, status, progress, message, error, created_at, updated_at FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return rows


_JOB_MANAGER: JobManager | None = None


def get_job_manager() -> JobManager:
    global _JOB_MANAGER
    if _JOB_MANAGER is None:
        _JOB_MANAGER = JobManager()
    return _JOB_MANAGER