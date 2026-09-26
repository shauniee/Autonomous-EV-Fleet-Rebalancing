from datetime import datetime, timedelta, timezone
import pytest


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
