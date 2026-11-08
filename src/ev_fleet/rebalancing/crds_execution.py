"""Bounded read-only distribution of the same pure CRDS calculation."""
import asyncio
import hashlib
import json
from time import perf_counter

import httpx

from ev_fleet.contracts.crds import CrdsEvaluation, CrdsEvaluationTask, CrdsTaskResult
from .crds import evaluate_moves


def task_fingerprint(task):
    return hashlib.sha256(json.dumps(task.model_dump(mode="json", exclude={"input_fingerprint"}),
                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def evaluate_candidates(client, settings, epoch_id, futures, moves, parameters, *, timeout_seconds):
    start = perf_counter()
    stats = {"mode": settings.execution_mode, "requests": 0, "retries": 0,
             "request_bytes": 0, "response_bytes": 0}

    def done(result):
        stats["elapsed_ms"] = (perf_counter() - start) * 1000
        return result, stats

    if timeout_seconds <= 0:
        return done(CrdsEvaluation(decision="DEFER", reason_code="DECISION_DEADLINE"))
    if settings.execution_mode == "LOCAL":
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(evaluate_moves, futures, moves, parameters),
                timeout=timeout_seconds - (perf_counter() - start))
        except asyncio.TimeoutError:
            return done(CrdsEvaluation(decision="DEFER", reason_code="DECISION_DEADLINE"))
        if perf_counter() - start > timeout_seconds:
            return done(CrdsEvaluation(decision="DEFER", reason_code="DECISION_DEADLINE"))
        return done(result)
    if not settings.worker_urls or not futures:
        return done(CrdsEvaluation(decision="DEFER", reason_code="WORKERS_UNAVAILABLE"))
    if len({f.future_id for f in futures}) != len(futures):
        return done(CrdsEvaluation(decision="DEFER", reason_code="INVALID_FUTURE_COVERAGE"))
    tasks = []
    for index, url in enumerate(settings.worker_urls):
        subset = futures[index::len(settings.worker_urls)]
        if not subset:
            continue
        ids = {f.future_id for f in subset}
        task = CrdsEvaluationTask(epoch_id=epoch_id, input_fingerprint="", parameters=parameters,
            futures=subset, moves=[m.model_copy(update={"outcomes": {
                key: value for key, value in m.outcomes.items() if key in ids}}) for m in moves])
        task.input_fingerprint = task_fingerprint(task)
        tasks.append((url, task))

    async def request(url, task):
        body = task.model_dump(mode="json")
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        for attempt in range(2):
            stats["requests"] += 1
            stats["request_bytes"] += len(payload)
            try:
                response = await client.post(url + "/api/v1/crds/evaluate", content=payload,
                                              headers={"Content-Type": "application/json"},
                                              timeout=settings.worker_timeout_seconds)
                stats["response_bytes"] += len(response.content)
                response.raise_for_status()
                reply = CrdsTaskResult.model_validate(response.json())
                if (reply.epoch_id != task.epoch_id or reply.model_version != task.model_version
                        or reply.input_fingerprint != task.input_fingerprint
                        or reply.future_ids != [f.future_id for f in task.futures]
                        or reply.evaluation.decision == "DEFER"):
                    raise ValueError("worker result identity or completeness mismatch")
                if len(reply.evaluation.ranked_candidates) != len(task.moves) or {(g.vehicle_id, g.facility_id) for g in reply.evaluation.ranked_candidates} != {
                        (m.vehicle_id, m.facility_id) for m in task.moves}:
                    raise ValueError("worker candidate coverage mismatch")
                if any(g.worst_case_future_id not in reply.future_ids for g in reply.evaluation.ranked_candidates):
                    raise ValueError("worker returned another future")
                return reply.evaluation
            except (httpx.HTTPError, ValueError):
                if attempt:
                    raise
                stats["retries"] += 1
                await asyncio.sleep(.05)

    try:
        replies = await asyncio.wait_for(asyncio.gather(*(request(url, task) for url, task in tasks)), timeout_seconds)
    except (asyncio.TimeoutError, httpx.HTTPError, ValueError):
        return done(CrdsEvaluation(decision="DEFER", reason_code="WORKER_RESULTS_INCOMPLETE"))
    grouped = {}
    for reply in replies:
        for gain in reply.ranked_candidates:
            grouped.setdefault((gain.vehicle_id, gain.facility_id), []).append(gain)
    ranked = []
    for gains in grouped.values():
        worst = min(gains, key=lambda g: (g.worst_case_gain_vehicle_minutes, g.worst_case_future_id))
        ranked.append(worst.model_copy(update={"worst_case_energy_required_kwh": max(
            g.worst_case_energy_required_kwh for g in gains)}))
    ranked.sort(key=lambda g: (-g.worst_case_gain_vehicle_minutes, g.worst_case_energy_required_kwh,
                               g.vehicle_id, g.facility_id))
    if not ranked or ranked[0].worst_case_gain_vehicle_minutes <= parameters.minimum_gain_vehicle_minutes:
        return done(CrdsEvaluation(decision="HOLD", reason_code="NO_USEFUL_MOVE", ranked_candidates=ranked))
    return done(CrdsEvaluation(decision="MOVE", reason_code="USEFUL_SHORTAGE_REDUCTION",
        selected_vehicle_id=ranked[0].vehicle_id, selected_facility_id=ranked[0].facility_id,
        ranked_candidates=ranked))
