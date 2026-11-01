"""Render whitelisted integration settings for local Kubernetes.

Pipe directly to kubectl; do not save or log the output. Values are read literally,
never executed or shell-expanded. Exported environment values override the file.
"""
import json
import os
from pathlib import Path
import sys

DEFAULTS = {
    "FACILITIES_SOURCE": "MOCK", "LTA_DATAMALL_ACCOUNT_KEY": "",
    "TRAFFIC_SOURCE": "DISABLED",
    "LTA_PARKING_REFRESH_SECONDS": "60", "LTA_CHARGING_REFRESH_SECONDS": "300",
    "LTA_PARKING_MAX_AGE_SECONDS": "180", "LTA_CHARGING_MAX_AGE_SECONDS": "900",
}
ROUTING_DEFAULTS = {"ROUTING_PROVIDER": "SIMULATED", "ONEMAP_ACCESS_TOKEN": "",
                    "OSRM_ROUTING_URL": "", "TRAFFIC_SOURCE": "DISABLED"}
TRAFFIC_DEFAULTS = {"TRAFFIC_SOURCE": "DISABLED", "LTA_DATAMALL_ACCOUNT_KEY": "",
                    "TRAFFIC_SIMULATION_ENABLED": "false",
                    "TRAFFIC_SIMULATION_CONTROL_KEY": ""}


def settings(path, defaults=DEFAULTS):
    values = dict(defaults)
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or key not in defaults:
                continue
            value = value.strip()
            if value[:1] in ("'", '"'):
                quote = value[0]
                end = value.find(quote, 1)
                if end == -1 or (value[end + 1:].strip() and not value[end + 1:].strip().startswith("#")):
                    raise ValueError(f"Invalid quoting for {key}")
                value = value[1:end]
            else:
                value = value.split(" #", 1)[0].rstrip()
            values[key] = value
    values.update({key: os.environ[key] for key in defaults if key in os.environ})
    if "FACILITIES_SOURCE" in values and values["FACILITIES_SOURCE"] not in ("MOCK", "LTA_DATAMALL"):
        raise ValueError("FACILITIES_SOURCE must be MOCK or LTA_DATAMALL")
    if values.get("TRAFFIC_SOURCE", "DISABLED") not in ("DISABLED", "LTA_DATAMALL"):
        raise ValueError("TRAFFIC_SOURCE must be DISABLED or LTA_DATAMALL")
    for key, minimum in (("LTA_PARKING_REFRESH_SECONDS", 60), ("LTA_CHARGING_REFRESH_SECONDS", 300),
                         ("LTA_PARKING_MAX_AGE_SECONDS", 60), ("LTA_CHARGING_MAX_AGE_SECONDS", 300)):
        if key in values and (not values[key].isdigit() or int(values[key]) < minimum):
            raise ValueError(f"{key} must be an integer >= {minimum}")
    if "ROUTING_PROVIDER" in values and values["ROUTING_PROVIDER"] not in ("SIMULATED", "ONEMAP"):
        raise ValueError("ROUTING_PROVIDER must be SIMULATED or ONEMAP")
    if ("TRAFFIC_SIMULATION_ENABLED" in values
            and values["TRAFFIC_SIMULATION_ENABLED"].lower() not in ("true", "false")):
        raise ValueError("TRAFFIC_SIMULATION_ENABLED must be true or false")
    return values


if __name__ == "__main__":
    arguments = sys.argv[1:]
    check = "--check" in arguments
    routing = "--routing" in arguments
    traffic_control = "--traffic-control" in arguments
    traffic_config = "--traffic-config" in arguments
    modes = sum((routing, traffic_control, traffic_config))
    if modes > 1:
        raise SystemExit("Choose only one render mode")
    arguments = [argument for argument in arguments
                 if argument not in ("--check", "--routing", "--traffic-control", "--traffic-config")]
    defaults = TRAFFIC_DEFAULTS if (traffic_control or traffic_config) else ROUTING_DEFAULTS if routing else DEFAULTS
    try:
        values = settings(Path(arguments[0] if arguments else ".env"), defaults)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if routing and values["ROUTING_PROVIDER"] == "ONEMAP" and not values["ONEMAP_ACCESS_TOKEN"].strip():
        raise SystemExit("ONEMAP_ACCESS_TOKEN is required when ROUTING_PROVIDER=ONEMAP")
    if not routing and values["TRAFFIC_SOURCE"] == "LTA_DATAMALL" and not values.get("LTA_DATAMALL_ACCOUNT_KEY", "").strip():
        raise SystemExit("LTA_DATAMALL_ACCOUNT_KEY is required when TRAFFIC_SOURCE=LTA_DATAMALL")
    if not (routing or traffic_control or traffic_config) and values["FACILITIES_SOURCE"] == "LTA_DATAMALL" and not values["LTA_DATAMALL_ACCOUNT_KEY"].strip():
        raise SystemExit("LTA_DATAMALL_ACCOUNT_KEY is required when FACILITIES_SOURCE=LTA_DATAMALL")
    if ((traffic_control or traffic_config)
            and values["TRAFFIC_SIMULATION_ENABLED"].lower() == "true"
            and len(values["TRAFFIC_SIMULATION_CONTROL_KEY"]) < 16):
        raise SystemExit("TRAFFIC_SIMULATION_CONTROL_KEY must contain at least 16 characters when simulation is enabled")
    if check:
        if routing:
            print("Routing provider: " + values["ROUTING_PROVIDER"])
        elif traffic_control or traffic_config:
            print("Traffic source: " + values["TRAFFIC_SOURCE"])
            print("Traffic simulation enabled: " + values["TRAFFIC_SIMULATION_ENABLED"].lower())
        else:
            print("Facilities source: " + values["FACILITIES_SOURCE"])
        if not (routing or traffic_control or traffic_config) and values["FACILITIES_SOURCE"] == "MOCK":
            print("Real parking/charging is disabled. Set FACILITIES_SOURCE=LTA_DATAMALL to enable it.")
        raise SystemExit(0)
    if traffic_config:
        print(json.dumps({"data": {
            "TRAFFIC_SOURCE": values["TRAFFIC_SOURCE"],
            "TRAFFIC_SIMULATION_ENABLED": values["TRAFFIC_SIMULATION_ENABLED"].lower(),
        }}))
        raise SystemExit(0)
    if traffic_control:
        secret_values = {"TRAFFIC_SIMULATION_CONTROL_KEY": values["TRAFFIC_SIMULATION_CONTROL_KEY"]}
        secret_name = "ev-fleet-traffic-control"
    else:
        secret_values = values
        secret_name = "ev-fleet-routing" if routing else "ev-fleet-facilities"
    print(json.dumps({
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": secret_name, "namespace": "ev-fleet"},
        "type": "Opaque", "stringData": secret_values,
    }))
