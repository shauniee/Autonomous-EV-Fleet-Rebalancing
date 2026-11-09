"""Local CRDS evaluation preserves event-loop responsiveness and deadlines."""
import asyncio
from threading import Event
from uuid import uuid4

import pytest

from ev_fleet.common.settings import CrdsSettings
from ev_fleet.contracts.crds import CrdsEvaluation, CrdsParameters
from ev_fleet.rebalancing import crds_execution


def test_local_evaluation_allows_another_coroutine_to_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release, finished = Event(), Event(), Event()

    def blocking_evaluate(futures, moves, parameters, **kwargs):
        started.set()
        release.wait(timeout=1)
        finished.set()
        return CrdsEvaluation(decision="HOLD", reason_code="NO_USEFUL_MOVE")

    monkeypatch.setattr(crds_execution, "evaluate_moves", blocking_evaluate)

    async def run():
        task = asyncio.create_task(crds_execution.evaluate_candidates(
            None, CrdsSettings(execution_mode="LOCAL"), uuid4(), [], [],
            CrdsParameters(), timeout_seconds=10))
        try:
            while not started.is_set():
                await asyncio.sleep(0)
            assert not finished.is_set(), "local evaluation blocked the event loop"
        finally:
            release.set()
            result, stats = await task
        assert result.decision == "HOLD"
        assert stats["mode"] == "LOCAL"

    asyncio.run(run())


def test_local_evaluation_returns_deadline_before_blocking_work_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, finished, cancelled = Event(), Event(), Event()

    def blocking_evaluate(futures, moves, parameters, **kwargs):
        stop = kwargs.get("cancelled", lambda: False)
        for _ in range(100):
            if stop():
                cancelled.set()
                break
            if release.wait(timeout=.01):
                break
        finished.set()
        return CrdsEvaluation(decision="HOLD", reason_code="NO_USEFUL_MOVE")

    monkeypatch.setattr(crds_execution, "evaluate_moves", blocking_evaluate)

    async def run():
        try:
            result, stats = await crds_execution.evaluate_candidates(
                None, CrdsSettings(execution_mode="LOCAL"), uuid4(), [], [],
                CrdsParameters(), timeout_seconds=.01)
            assert result.decision == "DEFER"
            assert result.reason_code == "DECISION_DEADLINE"
            assert await asyncio.to_thread(cancelled.wait, .5), "timed-out evaluator kept running"
            assert stats["mode"] == "LOCAL"
        finally:
            release.set()

    asyncio.run(run())
