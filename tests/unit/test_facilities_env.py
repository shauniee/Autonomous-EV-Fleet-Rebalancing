import pytest

from scripts.render_facilities_secret import TRAFFIC_DEFAULTS, settings


def test_env_loader_whitelists_settings_and_does_not_execute_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text('FACILITIES_SOURCE=LTA_DATAMALL\nLTA_DATAMALL_ACCOUNT_KEY="fixture$(secret)=key"\n'
                    'DATABASE_URL=private-database\nVITE_KEY=not-for-browser\n')
    result = settings(path)
    assert result["LTA_DATAMALL_ACCOUNT_KEY"] == "fixture$(secret)=key"
    assert result["LTA_PARKING_REFRESH_SECONDS"] == "60"
    assert "DATABASE_URL" not in result and "VITE_KEY" not in result


def test_env_loader_accepts_exports_comments_and_environment_overrides(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("export FACILITIES_SOURCE='LTA_DATAMALL' # comment\nLTA_DATAMALL_ACCOUNT_KEY=old\n")
    monkeypatch.setenv("LTA_DATAMALL_ACCOUNT_KEY", "fixture-key")
    assert settings(path)["LTA_DATAMALL_ACCOUNT_KEY"] == "fixture-key"


@pytest.mark.parametrize("line", ["FACILITIES_SOURCE=TYPO", "LTA_PARKING_REFRESH_SECONDS=1",
                                  "LTA_DATAMALL_ACCOUNT_KEY='unclosed"])
def test_invalid_configuration_fails_without_echoing_secrets(tmp_path, line):
    path = tmp_path / ".env"
    path.write_text(line)
    with pytest.raises(ValueError) as error:
        settings(path)
    assert "unclosed" not in str(error.value)


def test_traffic_settings_are_whitelisted_and_validated(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TRAFFIC_SOURCE=LTA_DATAMALL\nLTA_DATAMALL_ACCOUNT_KEY=fixture-key\n"
                    "TRAFFIC_SIMULATION_ENABLED=true\n"
                    "TRAFFIC_SIMULATION_CONTROL_KEY=fixture-control-key\nDATABASE_URL=private\n")
    result = settings(path, TRAFFIC_DEFAULTS)
    assert result["TRAFFIC_SOURCE"] == "LTA_DATAMALL"
    assert result["TRAFFIC_SIMULATION_ENABLED"] == "true"
    assert result["TRAFFIC_SIMULATION_CONTROL_KEY"] == "fixture-control-key"
    assert "DATABASE_URL" not in result


def test_invalid_traffic_boolean_fails_closed(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TRAFFIC_SIMULATION_ENABLED=yes\n")
    with pytest.raises(ValueError, match="must be true or false"):
        settings(path, TRAFFIC_DEFAULTS)
