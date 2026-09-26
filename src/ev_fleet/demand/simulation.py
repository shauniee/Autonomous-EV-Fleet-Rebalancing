import asyncio
import logging
import random


class DemandSimulator:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.random = random.Random(settings.random_seed)
        self.tick_count = 0
        self.last_tick_at = None
        self.last_error = None
        self.running = False

    def next_value(self, current):
        cfg = self.settings
        if current is None:
            return self.random.randint(cfg.min_demand, cfg.max_demand)
        return max(cfg.min_demand, min(cfg.max_demand, current + self.random.randint(-cfg.max_step, cfg.max_step)))

    async def tick(self):
        return await self.store.simulation_tick(self)

    async def run(self):
        self.running = True
        try:
            while self.settings.simulation_enabled:
                # Startup performs the initial tick before serving. Subsequent ticks wait.
                await asyncio.sleep(self.settings.update_interval_seconds)
                try:
                    await self.tick()
                    self.last_error = None
                except Exception:
                    self.last_error = "SIMULATION_TICK_FAILED"
                    logging.exception("demand simulation tick failed")
        finally:
            self.running = False

    async def status(self):
        snapshot = await self.store.composition_snapshot()
        return {"enabled": self.settings.simulation_enabled, "running": self.running,
                "tick_count": self.tick_count, "last_tick_at": self.last_tick_at,
                "last_error": self.last_error,
                "configuration": {key: getattr(self.settings, key) for key in (
                    "random_seed", "update_interval_seconds", "min_demand", "max_demand", "max_step", "manual_hold_seconds")},
                "zones": snapshot["provenance"]}
