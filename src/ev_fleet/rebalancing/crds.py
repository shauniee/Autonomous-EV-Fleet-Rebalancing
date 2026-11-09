"""Pure shortage and minimum marginal-gain calculation; no service access.

Callers supply eligible moves and complete baseline futures. A candidate's EV
must be included at its modeled origin throughout those baseline trajectories.
EVs outside all demand zones have no origin-zone stock to debit. Future
generation and integration with Fleet eligibility are separate responsibilities.
"""
from math import ceil, fsum

from ev_fleet.contracts.crds import (
    CrdsCandidateGain,
    CrdsEvaluation,
    CrdsFuture,
    CrdsMove,
    CrdsParameters,
)


def apply_move(future: CrdsFuture, parameters: CrdsParameters, move: CrdsMove) -> CrdsFuture:
    """Copy a future with one departure and conservatively rounded usable arrival."""
    for trajectory in future.zones.values():
        if len(trajectory.desired_stock) != parameters.slot_count:
            raise ValueError("future trajectories must match the configured horizon")
    arrival_slot = parameters.slot_count
    if (move.target_zone_id not in future.zones
            or move.origin_zone_id is not None and move.origin_zone_id not in future.zones):
        raise ValueError("move origin and target must exist in the future")
    if future.future_id not in move.outcomes:
        raise ValueError("move requires an outcome for the future")
    if move.origin_zone_id is not None and any(
            stock < 1 for stock in future.zones[move.origin_zone_id].usable_stock):
        raise ValueError("baseline origin stock must include the candidate EV in every slot")
    outcome = move.outcomes[future.future_id]
    ready_minutes = outcome.arrival_minutes + outcome.availability_delay_minutes
    if ready_minutes < parameters.horizon_minutes:
        arrival_slot = ceil(ready_minutes / parameters.slot_minutes)
    zones = {}
    for zone_id, trajectory in future.zones.items():
        usable = list(trajectory.usable_stock)
        if zone_id == move.origin_zone_id:
            usable = [stock - 1 for stock in usable]
        if zone_id == move.target_zone_id:
            usable = [stock + int(slot >= arrival_slot) for slot, stock in enumerate(usable)]
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


def evaluate_moves(futures: list[CrdsFuture], moves: list[CrdsMove],
                   parameters: CrdsParameters | None = None) -> CrdsEvaluation:
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
    baselines = {future.future_id: shortage_score(future, parameters) for future in futures}
    for move in moves:
        if set(move.outcomes) != future_ids:
            return CrdsEvaluation(decision="DEFER", reason_code="MOVE_FUTURES_MISMATCH")
        if (move.target_zone_id not in zones
                or move.origin_zone_id is not None and move.origin_zone_id not in zones):
            return CrdsEvaluation(decision="DEFER", reason_code="UNKNOWN_MOVE_ZONE")
        if move.origin_zone_id is not None and any(stock < 1 for future in futures
               for stock in future.zones[move.origin_zone_id].usable_stock):
            return CrdsEvaluation(decision="DEFER", reason_code="INCONSISTENT_ORIGIN_STOCK")

    ranked = []
    for move in moves:
        gains = []
        for future in futures:
            relocated = apply_move(future, parameters, move)
            gain = baselines[future.future_id] - shortage_score(relocated, parameters)
            gains.append((gain, future, relocated))
        gain, worst, relocated = min(gains, key=lambda row: (row[0], row[1].future_id))
        before = shortage_by_zone(worst, parameters)
        after = shortage_by_zone(relocated, parameters)
        ranked.append(CrdsCandidateGain(
            vehicle_id=move.vehicle_id, facility_id=move.facility_id,
            worst_case_gain_vehicle_minutes=gain, worst_case_future_id=worst.future_id,
            shortage_changes_by_zone={zone: after[zone] - before[zone] for zone in sorted(zones)},
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
