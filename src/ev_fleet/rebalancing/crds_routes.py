"""Arrival estimates using route progress and expiring synthetic segment caps."""
from datetime import datetime, timedelta

from ev_fleet.common.travel import distance_km
from .traffic import assess_traffic


def travel_minutes(route, traffic, now, *, distance_travelled_km=0, time_scale=1):
    # Returned minutes use the wall clock shared by demand and event expiry.
    if route.distance_km <= 0:
        return 0.0
    if traffic.get("source") != "SIMULATION":
        duration = route.duration_seconds
        if route.traffic_assessment is None:
            assessment = assess_traffic([p.model_dump() for p in route.geometry],
                                        route.distance_km, duration, traffic, now)
            if assessment["blocked"]:
                raise ValueError("known road block on existing route")
            duration = assessment["estimated_duration_seconds"] or duration
        return duration / (60 * time_scale) * max(0, 1 - distance_travelled_km / route.distance_km)
    lengths = [distance_km(a.model_dump(), b.model_dump())
               for a, b in zip(route.geometry, route.geometry[1:])]
    total = sum(lengths)
    if total <= 0 or route.duration_seconds <= 0:
        raise ValueError("invalid route timing")
    base_speed = route.distance_km / route.duration_seconds * 3600 * time_scale
    progress = max(0, distance_travelled_km)
    elapsed = 0.0
    for index, length in enumerate(lengths):
        segment = length / total * route.distance_km
        skipped = min(progress, segment)
        segment -= skipped
        progress -= skipped
        a, b = route.geometry[index:index + 2]
        events = []
        for event in traffic.get("simulated_events", []):
            forward = distance_km(a.model_dump(), event["start"]) <= .015 and distance_km(b.model_dump(), event["end"]) <= .015
            reverse = distance_km(b.model_dump(), event["start"]) <= .015 and distance_km(a.model_dump(), event["end"]) <= .015
            if forward or reverse:
                events.append(event)
        while segment > .000000001:
            when = now + timedelta(minutes=elapsed)
            active = [e for e in events if datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")) <= when
                      < datetime.fromisoformat(e["expires_at"].replace("Z", "+00:00"))]
            speed = min([base_speed] + [0 if e["kind"] == "ROAD_CLOSED" else e["speed_kph"] * time_scale for e in active])
            until_expiry = min(((datetime.fromisoformat(e["expires_at"].replace("Z", "+00:00")) - when).total_seconds() / 60
                                for e in active), default=float("inf"))
            minutes = segment / speed * 60 if speed > 0 else float("inf")
            step = min(minutes, until_expiry)
            if step == float("inf") or step <= 0:
                raise ValueError("unbounded route delay")
            segment = max(0, segment - speed * step / 60)
            elapsed += step
    return elapsed
