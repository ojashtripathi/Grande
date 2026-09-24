"""Background jobs with real progress and working cancellation.

The original animated a percentage with ``setInterval`` while the server worked,
so the bar reached 100% and sat there, and there was no way to stop a running
split. Here a job reports what it has actually finished, and Cancel takes effect
at the next batch boundary.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

logger = logging.getLogger("grande.jobs")


_EXPECTED: tuple[type[BaseException], ...] | None = None


def _is_expected(exc: BaseException) -> bool:
    """True when the error carries a message written for the person using the app.

    Imported lazily: the engine modules import this one, so importing them at
    module scope would be circular.
    """
    global _EXPECTED
    if _EXPECTED is None:
        from .export import ExportError
        from .ingest import IngestError
        from .pivot import PivotError
        from .sql import SqlError
        from .transform import TransformError

        _EXPECTED = (ExportError, IngestError, PivotError, SqlError, TransformError, ValueError)
    return isinstance(exc, _EXPECTED)

#: How long a finished job stays queryable before it is swept.
RETAIN_SECONDS = 900
#: Progress events buffered per job for clients that connect late or reconnect.
EVENT_BUFFER = 200


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = "queued"  # queued | running | done | error | cancelled
    percent: float = 0.0
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _events: deque = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER), repr=False)
    _wake: threading.Condition = field(default_factory=threading.Condition, repr=False)
    _seq: int = 0

    # ----------------------------------------------------------- cancellation

    def cancel(self) -> None:
        self._cancel.set()
        self.emit(status="cancelling", message="Stopping…")

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def cancel_token(self) -> threading.Event:
        """Passed into engine functions, which poll it between batches."""
        return self._cancel

    # --------------------------------------------------------------- progress

    def emit(self, **payload: Any) -> None:
        """Record a progress update and wake any listening stream."""
        if "percent" in payload and payload["percent"] is not None:
            self.percent = max(0.0, min(100.0, float(payload["percent"])))
        if payload.get("message"):
            self.message = str(payload["message"])
        if payload.get("status"):
            self.status = str(payload["status"])
        extra = {
            k: v for k, v in payload.items()
            if k not in {"percent", "message", "status"}
        }
        if extra:
            self.detail.update(extra)

        with self._wake:
            self._seq += 1
            self._events.append({
                "seq": self._seq,
                "at": time.time(),
                "status": self.status,
                "percent": round(self.percent, 1),
                "message": self.message,
                **extra,
            })
            self._wake.notify_all()

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "percent": round(self.percent, 1),
            "message": self.message,
            "detail": self.detail,
            "result": self.result,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
            "finished": self.status in {"done", "error", "cancelled"},
        }

    # ----------------------------------------------------------------- stream

    def stream(self, after: int = 0, timeout: float = 0.5) -> Iterator[dict[str, Any]]:
        """Yield progress events from ``after`` onward, ending when the job ends.

        A heartbeat is yielded on timeout so the HTTP connection stays warm and a
        disconnected client is noticed promptly.
        """
        cursor = after
        while True:
            with self._wake:
                pending = [e for e in self._events if e["seq"] > cursor]
                if not pending:
                    finished = self.status in {"done", "error", "cancelled"}
                    if finished:
                        yield {"seq": cursor, "status": self.status, "final": True,
                               "job": self.as_dict()}
                        return
                    self._wake.wait(timeout)
                    pending = [e for e in self._events if e["seq"] > cursor]
                    if not pending:
                        yield {"heartbeat": True}
                        continue
            for event in pending:
                cursor = event["seq"]
                yield event
            if self.status in {"done", "error", "cancelled"}:
                yield {"seq": cursor, "status": self.status, "final": True, "job": self.as_dict()}
                return


class JobRunner:
    """Runs jobs on daemon threads and keeps their state queryable."""

    def __init__(self, max_concurrent: int = 4):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._slots = threading.Semaphore(max_concurrent)

    def submit(
        self,
        kind: str,
        title: str,
        work: Callable[[Job], Any],
    ) -> Job:
        """Start ``work(job)`` on a background thread and return the job at once."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, title=title)
        with self._lock:
            self._sweep_locked()
            self._jobs[job.id] = job

        def run() -> None:
            self._slots.acquire()
            try:
                if job.cancelled:
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    job.emit(status="cancelled", message="Cancelled before starting", percent=0)
                    return
                job.started_at = time.time()
                job.emit(status="running", message=job.message or "Starting…", percent=0)
                result = work(job)
                if job.cancelled:
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    job.emit(status="cancelled", message="Cancelled", percent=job.percent)
                    return
                job.result = result
                job.status = "done"
                job.finished_at = time.time()
                job.emit(status="done", message="Finished", percent=100)
            except Exception as exc:
                # Anything deliberately raised for the user carries a written
                # message; everything else is an internal fault whose text may
                # name paths, SQL or library internals. Log the traceback where
                # the operator can see it and hand the browser a reference only.
                if _is_expected(exc):
                    job.error = str(exc) or exc.__class__.__name__
                else:
                    reference = uuid.uuid4().hex[:8]
                    logger.error(
                        "Job %s (%s) failed [ref %s]\n%s",
                        job.id, job.kind, reference, traceback.format_exc(),
                    )
                    job.error = (
                        "Something went wrong inside Grande. "
                        f"The details are in the Grande window (reference {reference})."
                    )
                job.status = "error"
                job.finished_at = time.time()
                job.emit(status="error", message=job.error, percent=job.percent)
            finally:
                self._slots.release()

        threading.Thread(target=run, name=f"grande-job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"No job {job_id!r}.")
        return job

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        job.cancel()
        return job

    def _sweep_locked(self) -> None:
        cutoff = time.time() - RETAIN_SECONDS
        stale = [
            jid for jid, job in self._jobs.items()
            if job.finished_at is not None and job.finished_at < cutoff
        ]
        for jid in stale:
            self._jobs.pop(jid, None)
