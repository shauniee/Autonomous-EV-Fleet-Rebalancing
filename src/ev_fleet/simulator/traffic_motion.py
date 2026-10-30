"""Pure speed cap for simulated edge EVs on actual approved road vertices.

No in-flight rerouting, off-road movement or dashboard-written positions.
"""
from bisect import bisect_right
from ev_fleet.common.travel import distance_km

def segment_speed(movement, nominal_speed_kph, snapshot):
    if (movement.state != "RELOCATING" or not movement.command
            or not movement.command.get("route") or not snapshot
            or snapshot.get("source") != "SIMULATION"):
        return nominal_speed_kph
    points = movement.command["route"]["geometry"]
    cumulative = movement.path_lengths
    if len(points) < 2 or len(cumulative) != len(points):
        return nominal_speed_kph
    # RoadRoute distance can differ from geometric vertex length slightly.
    geometry_progress = movement.distance / max(.00001, movement.total) * cumulative[-1]
    index = min(len(points)-2, max(0, bisect_right(cumulative, geometry_progress) - 1))
    speed = nominal_speed_kph
    for e in snapshot.get("simulated_events", []):
        if e["kind"] not in ("CONGESTION", "ROADWORKS", "ROAD_CLOSED"):
            continue
        # No cross-lane or arbitrary nearby road effects: both event vertices
        # must align with the EV's current segment, either direction.
        near_forward = (distance_km(points[index], e["start"]) <= .015
                        and distance_km(points[index+1], e["end"]) <= .015)
        near_reverse = (distance_km(points[index], e["end"]) <= .015
                        and distance_km(points[index+1], e["start"]) <= .015)
        if near_forward or near_reverse:
            if e["kind"] == "ROAD_CLOSED":
                return 0.0  # Freeze on road: operator must handle closure.
            speed = min(speed, e["speed_kph"])
    return speed
