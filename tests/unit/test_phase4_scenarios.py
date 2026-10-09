import pytest
from experiments.phase4.scenarios import SCENARIO_NAMES, generate_scenario


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_scenarios_are_deterministic_complete_and_paired(name):
    first = generate_scenario(name, 7)
    second = generate_scenario(name, 7)
    assert first == second
    assert first.state_hash() == second.state_hash()
    assert len(first.demands) == 6
    assert len({vehicle.vehicle_id for vehicle in first.vehicles}) == len(first.vehicles)


def test_seed_changes_scenario_without_changing_its_contract():
    first = generate_scenario("nearest_low_soc", 1)
    second = generate_scenario("nearest_low_soc", 2)
    assert first.state_hash() != second.state_hash()
    assert min(vehicle.soc_pct for vehicle in first.vehicles) < 35
    assert max(vehicle.soc_pct for vehicle in first.vehicles) > 80


def test_fault_and_concurrency_scenarios_declare_events():
    assert generate_scenario("disconnect_during_relocation", 0).disconnect_selected
    assert generate_scenario("changing_demand", 0).demand_change == 10
    assert generate_scenario("concurrent_requests", 0).concurrent_requests == 2
    assert any(not vehicle.online for vehicle in generate_scenario("offline_before_selection", 0).vehicles)

