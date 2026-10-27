import asyncio

import pytest

from thuauth.config import AuthConfig, Config
from thuauth.daemon import Daemon


class Health:
    def __init__(self, auto=True):
        self.config = Config(auth=AuthConfig(auto_relogin=auto, health_interval_seconds=0.01))
        self.started = asyncio.Event()
        self.ensures = self.queries = self.invalidations = 0

    async def ensure_all(self):
        self.ensures += 1
        self.started.set()

    async def status_all(self):
        self.queries += 1
        self.started.set()

    def invalidate(self):
        self.invalidations += 1


class Traffic:
    def __init__(self, events):
        self.events = events
        self.starts = self.resets = 0
        self.started = asyncio.Event()
        self.restarted = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run(self):
        self.starts += 1
        self.events.append("traffic_start")
        self.started.set()
        if self.starts > 1:
            self.restarted.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.events.append("traffic_stop")
            self.stopped.set()

    def reset(self):
        self.resets += 1
        self.events.append("traffic_reset")

    def snapshot(self):
        return {"bulk_bytes": 42}


@pytest.mark.asyncio
@pytest.mark.parametrize("auto", [True, False])
async def test_stop_cancels_workers_and_auto_relogin_can_be_disabled(auto):
    health, events = Health(auto), []
    traffic = Traffic(events)
    stop = asyncio.Event()

    async def reset():
        pytest.fail("normal stop must not reset sessions")

    daemon = Daemon(health, traffic, reset, monitor_interval=0.005)
    task = asyncio.create_task(daemon.run(stop))
    await asyncio.wait_for(traffic.started.wait(), 1)
    await asyncio.wait_for(health.started.wait(), 1)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert traffic.stopped.is_set()
    assert bool(health.ensures) is auto
    assert bool(health.queries) is not auto


@pytest.mark.asyncio
async def test_external_cancellation_waits_for_child_cleanup():
    health, events = Health(), []
    traffic = Traffic(events)

    async def reset():
        pass

    daemon = Daemon(health, traffic, reset, monitor_interval=0.005)
    task = asyncio.create_task(daemon.run(asyncio.Event()))
    await traffic.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert traffic.stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("return_normally", [False, True])
async def test_dead_worker_is_reported_and_siblings_are_cancelled(return_normally):
    health, events = Health(), []
    traffic = Traffic(events)

    async def broken():
        await traffic.started.wait()
        if not return_normally:
            raise RuntimeError("synthetic programming failure")

    health.ensure_all = broken

    async def reset():
        pass

    # A normally returning health operation is allowed; its outer health loop
    # is perpetual. Test unexpected normal exit on the traffic worker instead.
    if return_normally:

        async def finished_traffic():
            traffic.started.set()

        traffic.run = finished_traffic
    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(Daemon(health, traffic, reset).run(asyncio.Event()), 1)
    if not return_normally:
        assert traffic.stopped.is_set()


@pytest.mark.asyncio
async def test_resume_gap_stops_requests_before_reset_and_restarts_workers():
    health, events = Health(), []
    traffic = Traffic(events)
    now = [0.0]
    stop = asyncio.Event()

    async def reset():
        assert traffic.stopped.is_set()
        events.append("sessions_reset")

    daemon = Daemon(health, traffic, reset, clock=lambda: now[0], monitor_interval=0.005)
    task = asyncio.create_task(daemon.run(stop))
    await traffic.started.wait()
    now[0] = 120.0
    await asyncio.wait_for(traffic.restarted.wait(), 1)
    stop.set()
    await task
    assert health.invalidations == 1 and traffic.resets == 1
    assert events[:5] == [
        "traffic_start",
        "traffic_stop",
        "sessions_reset",
        "traffic_reset",
        "traffic_start",
    ]


@pytest.mark.asyncio
async def test_periodic_stats_are_emitted_without_task_leaks(caplog):
    health, events = Health(), []
    traffic = Traffic(events)
    stop = asyncio.Event()

    async def reset():
        pass

    caplog.set_level("INFO", logger="thuauth.daemon")
    task = asyncio.create_task(Daemon(health, traffic, reset, stats_interval=0.005).run(stop))
    await asyncio.sleep(0.025)
    stop.set()
    await task
    assert "bulk_bytes" in caplog.text
    assert traffic.stopped.is_set()


@pytest.mark.asyncio
async def test_unexpected_worker_cancellation_is_reported():
    health, traffic = Health(), Traffic([])

    async def cancelled_operation():
        await traffic.started.wait()
        child = asyncio.get_running_loop().create_future()
        child.cancel()
        await child

    health.ensure_all = cancelled_operation

    async def reset():
        pass

    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(Daemon(health, traffic, reset).run(asyncio.Event()), 0.2)
    assert traffic.stopped.is_set()
