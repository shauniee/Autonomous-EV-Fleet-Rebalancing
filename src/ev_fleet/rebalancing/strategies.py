"""Selection order only; eligibility and candidate metrics remain shared."""
from dataclasses import dataclass
from typing import Callable
from .policy import assess


@dataclass(frozen=True)
class SelectionStrategy:
    name: str
    policy_version: str
    sort_key: Callable[[dict], tuple]
    explanation: Callable[[dict], str]

    def rank(self, snapshot, target, settings, now):
        assessments = [assess(vehicle, target, settings, now) for vehicle in snapshot["vehicles"]]
        eligible = sorted((row for row in assessments if row["eligible"]), key=self.sort_key)
        excluded = sorted((row for row in assessments if not row["eligible"]),
                          key=lambda row: row["vehicle_id"])
        return eligible, excluded


ENERGY_AWARE = SelectionStrategy(
    name="energy_aware",
    policy_version="ev-aware-v1",
    sort_key=lambda row: (-row["score"], -row["predicted_arrival_soc_pct"],
                          row["distance_km"], row["vehicle_id"]),
    explanation=lambda winner: (
        f"{winner['vehicle_id']} has the highest eligible weighted score; "
        f"predicted arrival SOC is {winner['predicted_arrival_soc_pct']:.1f}%.")
)


NEAREST_EV = SelectionStrategy(
    name="nearest_ev",
    policy_version="nearest-eligible-v1",
    sort_key=lambda row: (row["distance_km"], row["vehicle_id"]),
    explanation=lambda winner: (
        f"{winner['vehicle_id']} is the geographically nearest eligible EV; "
        f"estimated relocation distance is {winner['distance_km']:.2f} km.")
)


STRATEGIES = {strategy.name: strategy for strategy in (ENERGY_AWARE, NEAREST_EV)}
