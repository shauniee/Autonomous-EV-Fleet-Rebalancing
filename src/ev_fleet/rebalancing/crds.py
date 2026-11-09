"""Pure shortage and minimum marginal-gain calculation; no service access.

Callers supply eligible moves and complete baseline futures. A candidate's EV
must be included at its modeled origin throughout those baseline trajectories.
EVs outside all demand zones have no origin-zone stock to debit. Future
generation and integration with Fleet eligibility are separate responsibilities.
"""
from collections.abc import Callable
from math import ceil, fsum

from ev_fleet.contracts.crds import (
    CrdsCandidateGain,
    CrdsEvaluation,
    CrdsFuture,
    CrdsMove,
    CrdsMoveOutcome,
    CrdsParameters,
    CrdsZoneTrajectory,
)


def _arrival_slot(outcome: CrdsMoveOutcome, parameters: CrdsParameters) -> int:
    ready_minutes = outcome.arrival_minutes + outcome.availability_delay_minutes
    if ready_minutes >= parameters.horizon_minutes:
        return parameters.slot_count
    return ceil(ready_minutes / parameters.slot_minutes)


def apply_move(future: CrdsFuture, parameters: CrdsParameters, move: CrdsMove) -> CrdsFuture:
    """Copy a future with one departure and conservatively rounded usable arrival."""
    for trajectory in future.zones.values():
        if len(trajectory.desired_stock) != parameters.slot_count:
            raise ValueError("future trajectories must match the configured horizon")
    if (move.target_zone_id not in future.zones
            or move.origin_zone_id is not None and move.origin_zone_id not in future.zones):
        raise ValueError("move origin and target must exist in the future")
    if future.future_id not in move.outcomes:
        raise ValueError("move requires an outcome for the future")
    if move.origin_zone_id is not None and any(
            stock < 1 for stock in future.zones[move.origin_zone_id].usable_stock):
        raise ValueError("baseline origin stock must include the candidate EV in every slot")
    arrival = _arrival_slot(move.outcomes[future.future_id], parameters)
    zones = {}
    for zone_id, trajectory in future.zones.items():
        usable = list(trajectory.usable_stock)
        if zone_id == move.origin_zone_id:
            usable = [stock - 1 for stock in usable]
        if zone_id == move.target_zone_id:
            usable = [stock + int(slot >= arrival) for slot, stock in enumerate(usable)]
        zones[zone_id] = trajectory.model_copy(update={"usable_stock": usable})
    return future.model_copy(update={"zones": zones})


def shortage_by_zone(future: CrdsFuture, parameters: CrdsParameters,
                     move: CrdsMove | None = None, *, weighted: bool = False) -> dict[str, float]:
    """Calculate shortage, optionally counting unmet priority coverage twice."""
    if any(len(t.desired_stock) != parameters.slot_count for t in future.zones.values()):
        raise ValueError("future trajectories must match the configured horizon")
    if move is not None:
        future = apply_move(future, parameters, move)
    priority_pct = parameters.priority_coverage_pct if weighted else 0
    return {zone: parameters.slot_minutes * fsum(
            max(0, desired - usable) + max(0, (desired * priority_pct + 99) // 100 - usable)
            for desired, usable in zip(t.desired_stock, t.usable_stock))
            for zone, t in future.zones.items()}


def shortage_score(future: CrdsFuture, parameters: CrdsParameters) -> float:
    return fsum(shortage_by_zone(future, parameters, weighted=True).values())


def _zone_deltas(trajectory: CrdsZoneTrajectory, priority_pct: int):
    """Prefix counts for one departure and one arrival's marginal effects."""
    departure = [(0, 0)]
    arrival = [(0, 0)]
    for desired, usable in zip(trajectory.desired_stock, trajectory.usable_stock):
        priority = (desired * priority_pct + 99) // 100
        raw, extra = departure[-1]
        departure.append((raw + (usable <= desired), extra + (usable <= priority)))
        raw, extra = arrival[-1]
        arrival.append((raw + (usable < desired), extra + (usable < priority)))
    return departure, arrival


def evaluate_moves(futures: list[CrdsFuture], moves: list[CrdsMove],
                   parameters: CrdsParameters | None = None, *,
                   cancelled: Callable[[], bool] | None = None) -> CrdsEvaluation:
    """Rank supplied moves by G and select one only above the useful-gain threshold.

    Missing or inconsistent coverage yields DEFER, never a partial minimum.
    Malformed dimensions and duplicate identities raise ValueError instead.
    Eligibility, facility capacity and batch admission are not performed here.
    """
    parameters = parameters if parameters is not None else CrdsParameters()
    if not futures:
        return CrdsEvaluation(decision="DEFER", reason_code="NO_FUTURES")
    future_ids = {future.future_id for future in futures}
    if len(future_ids) != len(futures):
        raise ValueError("future IDs must be unique")
    move_ids = {(move.vehicle_id, move.facility_id) for move in moves}
    if len(move_ids) != len(moves):
        raise ValueError("vehicle-to-facility candidate pairs must be unique")

    zones = set(futures[0].zones)
    if any(set(future.zones) != zones for future in futures):
        return CrdsEvaluation(decision="DEFER", reason_code="INCOMPLETE_ZONE_COVERAGE")
    deltas = {}
    for future in futures:
        if cancelled is not None and cancelled():
            return CrdsEvaluation(decision="DEFER", reason_code="DECISION_DEADLINE")
        if any(len(t.desired_stock) != parameters.slot_count for t in future.zones.values()):
            raise ValueError("future trajectories must match the configured horizon")
        deltas[future.future_id] = {
            zone: _zone_deltas(t, parameters.priority_coverage_pct)
            for zone, t in future.zones.items()
        }
    for move in moves:
        if cancelled is not None and cancelled():
            return CrdsEvaluation(decision="DEFER", reason_code="DECISION_DEADLINE")
        if set(move.outcomes) != future_ids:
            return CrdsEvaluation(decision="DEFER", reason_code="MOVE_FUTURES_MISMATCH")
        if (move.target_zone_id not in zones
                or move.origin_zone_id is not None and move.origin_zone_id not in zones):
            return CrdsEvaluation(decision="DEFER", reason_code="UNKNOWN_MOVE_ZONE")
        if move.origin_zone_id is not None and any(stock < 1 for future in futures
               for stock in future.zones[move.origin_zone_id].usable_stock):
            return CrdsEvaluation(decision="DEFER", reason_code="INCONSISTENT_ORIGIN_STOCK")

    ranked = []
    zone_order = sorted(zones)
    for move in moves:
        if cancelled is not None and cancelled():
            return CrdsEvaluation(decision="DEFER", reason_code="DECISION_DEADLINE")
        gains = []
        origin, target = move.origin_zone_id, move.target_zone_id
        for future in futures:
            by_zone = deltas[future.future_id]
            arrival = _arrival_slot(move.outcomes[future.future_id], parameters)
            lost = lost_priority = gained = gained_priority = 0
            if origin is not None:
                departures, _ = by_zone[origin]
                # A move within one zone restores the same EV on arrival.
                lost, lost_priority = departures[arrival if origin == target else -1]
            if origin != target:
                _, arrivals = by_zone[target]
                gained = arrivals[-1][0] - arrivals[arrival][0]
                gained_priority = arrivals[-1][1] - arrivals[arrival][1]
            gain = parameters.slot_minutes * (gained + gained_priority - lost - lost_priority)
            gains.append((gain, future.future_id, lost, gained))
        gain, worst, lost, gained = min(gains)
        changes = dict.fromkeys(zone_order, 0)
        if origin is not None:
            changes[origin] = lost * parameters.slot_minutes
        changes[target] -= gained * parameters.slot_minutes
        ranked.append(CrdsCandidateGain(
            vehicle_id=move.vehicle_id, facility_id=move.facility_id,
            worst_case_gain_vehicle_minutes=gain, worst_case_future_id=worst,
            shortage_changes_by_zone=changes,
            worst_case_energy_required_kwh=max(
                outcome.energy_required_kwh for outcome in move.outcomes.values())))
    ranked.sort(key=lambda row: (-row.worst_case_gain_vehicle_minutes,
                                row.worst_case_energy_required_kwh,
                                row.vehicle_id, row.facility_id))
    if not ranked or ranked[0].worst_case_gain_vehicle_minutes <= parameters.minimum_gain_vehicle_minutes:
        return CrdsEvaluation(decision="HOLD", reason_code="NO_USEFUL_MOVE", ranked_candidates=ranked)
    winner = ranked[0]
    return CrdsEvaluation(
        decision="MOVE", reason_code="USEFUL_SHORTAGE_REDUCTION",
        selected_vehicle_id=winner.vehicle_id, selected_facility_id=winner.facility_id,
        ranked_candidates=ranked)
