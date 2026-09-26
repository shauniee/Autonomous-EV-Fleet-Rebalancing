import pytest
from pydantic import ValidationError
from ev_fleet.demand.aggregation import balances, demand_level
from ev_fleet.demand.settings import DemandSettings


@pytest.mark.parametrize("demand,available,inbound,shortage,surplus,projected,severity", [
    (12, 12, 0, 0, 0, 0, "NORMAL"), (12, 8, 3, 4, 0, 1, "WARNING"),
    (12, 6, 6, 6, 0, 0, "CRITICAL"), (3, 5, 0, 0, 2, 0, "NORMAL"),
    (0, 0, 0, 0, 0, 0, "NORMAL"), (0, 3, 2, 0, 3, 0, "NORMAL"),
    (1, 0, 0, 1, 0, 1, "CRITICAL"), (10, 6, 0, 4, 0, 4, "WARNING"),
    (10, 5, 0, 5, 0, 5, "CRITICAL")])
def test_balances_and_boundaries(demand, available, inbound, shortage, surplus, projected, severity):
    result = balances(demand, available, inbound)
    assert result["current_balance"] == demand - available
    assert result["current_shortage"] == shortage and result["current_surplus"] == surplus
    assert result["projected_shortage"] == projected
    assert result["projected_surplus"] == max(available + inbound - demand, 0)
    assert result["hotspot_severity"] == severity
    assert result["shortage_ratio"] == (shortage / demand if demand else 0)


@pytest.mark.parametrize("value,level", [(0, "LOW"), (4, "LOW"), (5, "MEDIUM"), (9, "MEDIUM"), (10, "HIGH")])
def test_demand_levels(value, level):
    assert demand_level(value, DemandSettings()) == level


def test_custom_critical_boundary():
    assert balances(10, 7, 0, .3)["hotspot_severity"] == "CRITICAL"
    assert balances(10, 8, 0, .3)["hotspot_severity"] == "WARNING"


@pytest.mark.parametrize("values", [{"min_demand": 5, "max_demand": 4}, {"medium_demand": 10, "high_demand": 10},
    {"update_interval_seconds": 0}, {"critical_shortage_ratio": 0}, {"zone_radius_km": float("nan")}])
def test_invalid_configuration(values):
    with pytest.raises(ValidationError):
        DemandSettings(**values)
