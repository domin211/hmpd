"""Per-controller serial FIFO: one worker thread executes queued jobs with retry/backoff.

Serial controllers can only handle one command at a time, so each controller gets its
own queue and worker thread. The queue itself doesn't know what a job "means" (temps
sync, regs sync, or a set command) -- it just calls the supplied `execute` callback and
handles command-gap pacing, retries, and thread lifecycle generically.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence

from .models import ControllerJob

log = logging.getLogger("hmpd_bridge.queue")


class ControllerQueue:
    def __init__(
        self,
        label: str,
        execute: Callable[[ControllerJob], None],
        *,
        command_gap_seconds: float,
        max_attempts: int,
        retry_delays: Sequence[float],
    ) -> None:
        self.label = label
        self._execute = execute
        self.command_gap_seconds = command_gap_seconds
        self.max_attempts = max_attempts
        self.retry_delays = list(retry_delays)

        self._condition = threading.Condition()
        self._items: list[ControllerJob] = []
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._current_kind: str | None = None
        self._last_finished_at = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker_loop, name=f"hmpd_worker_{self.label}", daemon=False)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def restart_if_dead(self) -> None:
        if self._thread is not None and not self._thread.is_alive():
            log.critical("Worker thread for %s is dead, restarting", self.label)
            self.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._shutdown.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("Worker thread %s did not exit cleanly", self.label)

    def enqueue(self, job: ControllerJob, wait: bool = False) -> list[str]:
        with self._condition:
            self._items.append(job)
            queue_len = len(self._items)
            self._condition.notify()
        log.debug("Enqueued %s for %s reason=%s queue_len=%s", job.kind, self.label, job.reason or "-", queue_len)

        if not wait:
            return []

        job.done.wait()
        if job.error is not None:
            error, job.error = job.error, None
            raise error
        return job.result_lines

    def pending_set_count(self) -> int:
        with self._condition:
            return sum(1 for job in self._items if job.kind == "set")

    def is_kind_active_or_queued(self, kind: str) -> bool:
        if self._current_kind == kind:
            return True
        with self._condition:
            return any(job.kind == kind for job in self._items)

    def _worker_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                with self._condition:
                    while not self._items and not self._shutdown.is_set():
                        self._condition.wait(timeout=1.0)
                    if self._shutdown.is_set():
                        break
                    job = self._items.pop(0)

                self._current_kind = job.kind
                try:
                    self._execute_with_retries(job)
                except Exception as exc:  # noqa: BLE001 - job failure is reported via job.error
                    job.error = exc
                    log.error(
                        "Queue job failed permanently kind=%s controller=%s reason=%s err=%s",
                        job.kind,
                        self.label,
                        job.reason or "-",
                        exc,
                        exc_info=True,
                    )
                finally:
                    self._last_finished_at = time.monotonic()
                    self._current_kind = None
                    job.done.set()
            except Exception:  # noqa: BLE001 - never let the worker thread die silently
                log.critical("Unhandled exception in controller worker %s", self.label, exc_info=True)
                time.sleep(5.0)
        log.info("Controller worker loop exiting for %s", self.label)

    def _enforce_command_gap(self) -> None:
        remaining = self.command_gap_seconds - (time.monotonic() - self._last_finished_at)
        if remaining > 0:
            time.sleep(remaining)

    def _execute_with_retries(self, job: ControllerJob) -> None:
        last_exc: BaseException | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                self._enforce_command_gap()
                self._execute(job)
                return
            except Exception as exc:  # noqa: BLE001 - retried below, re-raised after last attempt
                last_exc = exc
                if attempt >= self.max_attempts:
                    break
                delay = self.retry_delays[min(attempt - 1, len(self.retry_delays) - 1)]
                log.warning(
                    "Command failed attempt %s/%s for %s kind=%s reason=%s: %s. Retrying in %ss",
                    attempt,
                    self.max_attempts,
                    self.label,
                    job.kind,
                    job.reason or "-",
                    exc,
                    delay,
                )
                time.sleep(delay)

        log.critical(
            "Command failed after %s attempts for %s kind=%s reason=%s action=%s last_error=%s",
            self.max_attempts,
            self.label,
            job.kind,
            job.reason or "-",
            job.action_args,
            last_exc,
        )
        raise RuntimeError(
            f"Command failed after {self.max_attempts} attempts for {self.label} "
            f"kind={job.kind} reason={job.reason or '-'}: {last_exc}"
        )
