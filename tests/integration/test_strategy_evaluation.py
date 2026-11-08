import asyncio
from experiments.strategy_evaluation.cluster import execute_trial
from experiments.strategy_evaluation.config import EvaluationConfig
from experiments.strategy_evaluation.scenarios import generate_scenario
from ev_fleet.rebalancing.strategies import ENERGY_AWARE, NEAREST_EV


def run(name, strategy):
    return asyncio.run(execute_trial(generate_scenario(name, 0), strategy,
                                     EvaluationConfig(repetitions=1)))


def test_paired_trial_changes_low_soc_selection_without_changing_initial_state():
    energy = run("nearest_low_soc", ENERGY_AWARE)
    nearest = run("nearest_low_soc", NEAREST_EV)
    assert energy["initial_state_hash"] == nearest["initial_state_hash"]
    assert energy["selected_vehicle_id"] != nearest["selected_vehicle_id"]
    assert energy["actual_arrival_soc_pct"] > nearest["actual_arrival_soc_pct"]
    assert not energy["operationally_poor_outcome"] and nearest["actual_low_arrival_soc"]
    assert energy["final_dispatch_state"] == nearest["final_dispatch_state"] == "SUCCEEDED"


def test_disconnect_is_recorded_and_recovers_with_same_dispatch():
    result = run("disconnect_during_relocation", ENERGY_AWARE)
    assert "UNKNOWN" in result["transient_dispatch_states"]
    assert result["final_dispatch_state"] == "SUCCEEDED"
    assert result["failed_or_aborted_relocations"] == 0


def test_concurrent_requests_preserve_single_vehicle_claim():
    result = run("concurrent_requests", ENERGY_AWARE)
    assert result["request_count"] == 2
    assert result["successful_relocations"] == 1
    assert result["failed_or_aborted_relocations"] == 1
    assert result["failure_reason"] in {
        "VEHICLE_VERSION_CONFLICT", "VEHICLE_NOT_ELIGIBLE:BUSY"
    }
