import random
import pytest

from ev_fleet.simulations.customer import (
    ZONES, PICKUP_WEIGHTS, RentalMovement, choose_destination, zone_for,
)


def test_scenario_destinations_are_seeded_and_not_same_as_origin():
    first = random.Random(42)
    second = random.Random(42)
    a = [choose_destination("woodlands", "morning", first) for _ in range(30)]
    b = [choose_destination("woodlands", "morning", second) for _ in range(30)]
    assert a == b
    assert all(zone in ZONES and zone != "woodlands" for zone in a)
    assert a.count("cbd") > a.count("tampines")
    assert all(all(value > 0 for value in weights.values()) for weights in PICKUP_WEIGHTS.values())


def test_nearest_zone_and_customer_movement_reserve():
    motion = RentalMovement(ZONES["punggol"], 85)
    assert zone_for(motion.position) == "punggol"
    assert motion.can_rent_to(ZONES["cbd"])
    motion.start_customer_trip("rental-1", ZONES["cbd"])
    assert motion.telemetry()["relocation_state"] == "IDLE"
    assert motion.telemetry()["availability"] == "UNAVAILABLE"
    with pytest.raises(ValueError):
        motion.start_customer_trip("rental-2", ZONES["bishan"])
    motion.advance(60, 30)
    assert motion.position != ZONES["punggol"]
    assert 0 < motion.soc_pct < 85
    motion.advance(100000, 30)
    assert motion.rental["arrived"]
    assert motion.position == ZONES["cbd"]
    with pytest.raises(ValueError):
        motion.start_customer_trip("rental-3", ZONES["bishan"])
    motion.finish_customer_trip()
    assert motion.telemetry()["availability"] == "AVAILABLE"
    assert zone_for(motion.position) == "cbd"


def test_customer_trip_safety_and_invalid_movement():
    motion = RentalMovement(ZONES["woodlands"], 20)
    assert not motion.can_rent_to(ZONES["cbd"])
    with pytest.raises(ValueError):
        motion.start_customer_trip("rental", ZONES["cbd"])
    with pytest.raises(ValueError):
        motion.finish_customer_trip()
    with pytest.raises(ValueError):
        motion.advance(-1, 30)


def test_customer_rental_http_lifecycle_and_relocation_exclusion():
    """Exercise real Fleet rental endpoints with a sole-telemetry-owner EV."""
    import asyncio
    import httpx
    from ev_fleet.fleet.app import create_app
    from ev_fleet.fleet.store import FleetStore
    from ev_fleet.simulations.customer import RentalVehicle

    async def exercise():
        store = FleetStore()
        await store.start()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store))) as client:
            node = RentalVehicle("http://fleet.test", "http://dispatch.test", "customer-ev-test",
                                 ZONES["punggol"], 85, client=client, tick_seconds=0.01)
            await node.register()
            await node.send_telemetry()
            assert (await store.get("customer-ev-test"))["effective_availability"] == "AVAILABLE"
            task = asyncio.create_task(node.run_customer_trip("cbd"))
            for _ in range(200):
                if node.movement.rental is not None:
                    break
                await asyncio.sleep(0.01)
            assert node.movement.rental is not None, "Rental was not accepted"
            during = await store.get("customer-ev-test")
            assert during["active_rental"] is not None
            assert during["effective_availability"] == "UNAVAILABLE"
            # A second owner must fail closed, without taking this car's session.
            another = RentalVehicle("http://fleet.test", "http://dispatch.test",
                                    "customer-ev-test", ZONES["punggol"], 85, client=client)
            with pytest.raises(RuntimeError, match="unresolved journey"):
                await another.register()
            node.movement.advance(100000, 30)
            assert await asyncio.wait_for(task, timeout=5) == "returned"
            after = await store.get("customer-ev-test")
            assert after["active_rental"] is None
            assert after["effective_availability"] == "AVAILABLE"
            assert after["telemetry"]["position"] == ZONES["cbd"]
            assert after["telemetry"]["soc_pct"] < during["telemetry"]["soc_pct"]
        await store.close()

    asyncio.run(exercise())
