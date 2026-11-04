import pytest

from ev_fleet.simulator import cli


def test_facilities_grace_defaults_from_environment_and_allows_zero(monkeypatch):
    monkeypatch.setenv("FACILITIES_FAILURE_GRACE_SECONDS", "10")
    assert cli.parse_args([]).facilities_grace_seconds == 10
    assert cli.parse_args(["--facilities-grace-seconds", "0"]).facilities_grace_seconds == 0


@pytest.mark.parametrize("value", ["-0.001", "nan", "inf"])
def test_facilities_grace_rejects_negative_or_non_finite_values(value):
    with pytest.raises(SystemExit) as error:
        cli.parse_args(["--facilities-grace-seconds", value])
    assert error.value.code == 2
