"""Remove deployment configuration before importing or spawning test apps."""
import os
from pathlib import Path


_PREFIXES = (
    "ANALYTICS_", "CRDS_", "DATABASE_", "DEMAND_", "FACILITIES_", "KUBERNETES_",
    "LTA_", "ONEMAP_", "OSRM_", "PG", "RENTAL_", "ROUTING_", "SIMULATOR_",
    "TRAFFIC_",
)
_NAMES = {
    "ACK_TIMEOUT_SECONDS", "ALL_PROXY", "ARRIVAL_TOLERANCE_KM",
    "CHARGING_MAX_AGE_SECONDS", "DASHBOARD_CACHE_ENABLED", "DISPATCH_URL",
    "DISTANCE_SCALE_KM", "ENERGY_MARGIN_FACTOR", "FLEET_URL", "HTTP_PROXY",
    "HTTPS_PROXY", "KUBECONFIG", "MIN_RESERVE_PCT", "NO_PROXY", "OFFLINE_SECONDS",
    "PROGRESS_TIMEOUT_SECONDS", "PYTHONPATH", "REBALANCING_URL",
    "RECOMMENDATION_TTL_SECONDS", "RECONCILE_INTERVAL_SECONDS", "REDIS_URL",
    "ROUTE_FACTOR", "SIMULATION_TIME_SCALE", "STALE_SECONDS",
    "TELEMETRY_INTERVAL_SECONDS", "TELEMETRY_TEST_DATABASE_URL",
}


def is_deployment_setting(name: str) -> bool:
    name = name.upper()
    return name in _NAMES or name.startswith(_PREFIXES)


def service_test_environment(root: Path, **overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not is_deployment_setting(key)}
    # An unavailable loopback port keeps unused dependencies away from any
    # already running Compose services or Kubernetes port forwards.
    return {
        **env,
        "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root))),
        "KUBECONFIG": os.devnull,
        "CRDS_MODE": "DISABLED",
        "FLEET_URL": "http://127.0.0.1:0",
        "DEMAND_URL": "http://127.0.0.1:0",
        "REBALANCING_URL": "http://127.0.0.1:0",
        "DISPATCH_URL": "http://127.0.0.1:0",
        # The simulator treats a nonempty facilities URL as an opt-in to
        # parking and charging. Tests with a facilities server override this.
        "FACILITIES_URL": "",
        **overrides,
    }
