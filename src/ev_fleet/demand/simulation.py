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
        self._lifecycle_lock = asyncio.Lock()
        self._task = None
        self._stop_event = asyncio.Event()

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
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), self.settings.update_interval_seconds)
                except asyncio.TimeoutError:
                    pass
                if self._stop_event.is_set() or not self.settings.simulation_enabled:
                    break
                try:
                    await self.tick()
                    self.last_error = None
                except Exception:
                    self.last_error = "SIMULATION_TICK_FAILED"
                    logging.exception("demand simulation tick failed")
        finally:
            self.running = False

    async def _start(self, persist_choice):
        async with self._lifecycle_lock:
            if self._task is not None and not self._task.done():
                return await self.status()
            self.settings.simulation_enabled = True
            self._stop_event = asyncio.Event()
            try:
                await self.tick()
                if persist_choice:
                    await self.store.set_simulation_enabled(True)
            except Exception:
                self.settings.simulation_enabled = False
                self._stop_event.set()
                raise
            self.running = True
            self._task = asyncio.create_task(self.run())
            return await self.status()

    async def start(self):
        return await self._start(persist_choice=True)

    async def resume_if_enabled(self):
        if not self.store.simulation_enabled:
            return await self.status()
        return await self._start(persist_choice=False)

    async def _stop(self, persist_choice):
        async with self._lifecycle_lock:
            self.settings.simulation_enabled = False
            task, self._task = self._task, None
            self._stop_event.set()
            if task is not None and not task.done():
                await task
            self.running = False
            if persist_choice:
                await self.store.set_simulation_enabled(False)
            return await self.status()

    async def pause(self):
        return await self._stop(persist_choice=True)

    async def close(self):
        await self._stop(persist_choice=False)

    async def status(self):
        snapshot = await self.store.composition_snapshot()
        return {"enabled": self.settings.simulation_enabled, "running": self.running,
                "tick_count": self.tick_count, "last_tick_at": self.last_tick_at,
                "last_error": self.last_error,
                "configuration": {key: getattr(self.settings, key) for key in (
                    "random_seed", "update_interval_seconds", "min_demand", "max_demand", "max_step", "manual_hold_seconds")},
                "zones": snapshot["provenance"]}
