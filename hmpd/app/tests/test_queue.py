import threading
import time

from hmpd_bridge.models import ControllerJob
from hmpd_bridge.queue import ControllerQueue


def make_queue(execute, max_attempts=3, retry_delays=(0.01, 0.01, 0.01)) -> ControllerQueue:
    return ControllerQueue(
        label="test",
        execute=execute,
        command_gap_seconds=0.0,
        max_attempts=max_attempts,
        retry_delays=retry_delays,
    )


def test_enqueue_wait_returns_result_lines():
    def execute(job: ControllerJob) -> None:
        job.result_lines = ["ok"]

    queue = make_queue(execute)
    queue.start()
    try:
        result = queue.enqueue(ControllerJob(kind="temps"), wait=True)
        assert result == ["ok"]
    finally:
        queue.stop(timeout=2)


def test_enqueue_retries_then_succeeds():
    attempts = []

    def execute(job: ControllerJob) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("transient")

    queue = make_queue(execute, max_attempts=5)
    queue.start()
    try:
        queue.enqueue(ControllerJob(kind="regs"), wait=True)
        assert len(attempts) == 3
    finally:
        queue.stop(timeout=2)


def test_enqueue_raises_after_max_attempts():
    def execute(job: ControllerJob) -> None:
        raise RuntimeError("boom")

    queue = make_queue(execute, max_attempts=2, retry_delays=(0.01,))
    queue.start()
    try:
        try:
            queue.enqueue(ControllerJob(kind="regs"), wait=True)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "boom" in str(exc)
    finally:
        queue.stop(timeout=2)


def test_is_kind_active_or_queued_reflects_pending_jobs():
    started = threading.Event()
    release = threading.Event()

    def execute(job: ControllerJob) -> None:
        started.set()
        release.wait(timeout=2)

    queue = make_queue(execute)
    queue.start()
    try:
        queue.enqueue(ControllerJob(kind="set", zone_unique_id="z1"), wait=False)
        assert started.wait(timeout=2)
        assert queue.is_kind_active_or_queued("set") is True
        assert queue.is_kind_active_or_queued("temps") is False
    finally:
        release.set()
        queue.stop(timeout=2)


def test_command_gap_is_enforced_between_jobs():
    timestamps = []

    def execute(job: ControllerJob) -> None:
        timestamps.append(time.monotonic())

    queue = ControllerQueue(
        label="gap-test",
        execute=execute,
        command_gap_seconds=0.2,
        max_attempts=1,
        retry_delays=(),
    )
    queue.start()
    try:
        queue.enqueue(ControllerJob(kind="temps"), wait=True)
        queue.enqueue(ControllerJob(kind="temps"), wait=True)
        assert timestamps[1] - timestamps[0] >= 0.2
    finally:
        queue.stop(timeout=2)
