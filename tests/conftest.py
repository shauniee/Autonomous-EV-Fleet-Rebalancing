from datetime import datetime, timedelta, timezone
import os
from urllib.parse import urlsplit

import pytest

from scripts.isolated_test_environment import is_deployment_setting


_TELEMETRY_URL = pytest.StashKey[str]()


def pytest_addoption(parser):
    parser.addoption("--telemetry-postgres", action="store_true",
                     help="Run telemetry integration against the disposable loopback test database.")


def pytest_configure(config):
    if config.getoption("--telemetry-postgres"):
        url = os.environ.get("TELEMETRY_TEST_DATABASE_URL", "")
        try:
            target = urlsplit(url)
            valid = (
                target.scheme in {"postgres", "postgresql"}
                and target.hostname in {"127.0.0.1", "::1"}
                and target.port is not None and target.port > 0
                and target.path == "/twin_history_test"
                and not target.query and not target.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise pytest.UsageError(
                "--telemetry-postgres requires TELEMETRY_TEST_DATABASE_URL pointing "
                "to the disposable twin_history_test database on an explicit loopback port."
            )
        config.stash[_TELEMETRY_URL] = url

    # App modules create configured instances during collection, before
    # function-scoped fixtures would be able to remove inherited settings.
    environment = pytest.MonkeyPatch()
    config.add_cleanup(environment.undo)
    for name in tuple(os.environ):
        if is_deployment_setting(name):
            environment.delenv(name)
    environment.setenv("KUBECONFIG", os.devnull)


@pytest.fixture
def telemetry_database_url(pytestconfig):
    if _TELEMETRY_URL not in pytestconfig.stash:
        pytest.skip("Disposable PostgreSQL check requires --telemetry-postgres")
    return pytestconfig.stash[_TELEMETRY_URL]


class FakeClock:
    def __init__(self):
        self.elapsed = 0
        self.origin = datetime(2026, 9, 26, tzinfo=timezone.utc)

    def now(self):
        return self.origin + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds


@pytest.fixture
def clock():
    return FakeClock()
