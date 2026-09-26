import time
from ev_fleet.contracts.common import utcnow


class Clock:
    def now(self):
        return utcnow()

    def monotonic(self):
        return time.monotonic()
