"""Synthetic disruptions affect only the road-aligned edge simulator."""
from ev_fleet.simulator.movement import Movement
from ev_fleet.simulator.traffic_motion import segment_speed
from ev_fleet.common.travel import distance_km

A = {"latitude":1.30,"longitude":103.80}
B = {"latitude":1.305,"longitude":103.80}
C = {"latitude":1.305,"longitude":103.81}

def moving():
    model = Movement(A, 80)
    model.state = "RELOCATING"
    model.command = {"dispatch_id":"dispatch-a", "route":{"geometry":[A,B,C]}}
    model.path_lengths = [0, distance_km(A,B), distance_km(A,B) + distance_km(B,C)]
    model.total = model.path_lengths[-1]
    model.distance = 0
    return model

def feed(kind="CONGESTION", speed=12, start=A, end=B):
    return {"source":"SIMULATION", "status":"FRESH",
            "simulated_events":[{"id":"test", "kind":kind, "speed_kph":speed,
                                 "start":start, "end":end}]}

def test_simulated_congestion_caps_real_movement_speed_only_on_affected_segment():
    m = moving()
    assert segment_speed(m,30,feed()) == 12
    m.advance(30, segment_speed(m,30,feed()))
    assert m.speed_kph == 12
    assert m.odometer_km > 0
    m.distance = m.path_lengths[1] + .05
    assert segment_speed(m,30,feed()) == 30

def test_explicit_closure_halts_without_inventing_a_route_and_resumes_when_cleared():
    m = moving()
    assert segment_speed(m,30,feed("ROAD_CLOSED",None)) == 0
    assert segment_speed(m,30,{"source":"SIMULATION","simulated_events":[]}) == 30
    # A separate parallel street cannot inherit a synthetic road closure.
    unrelated = feed("ROAD_CLOSED",None,
                     {"latitude":1.31,"longitude":103.81},
                     {"latitude":1.32,"longitude":103.81})
    assert segment_speed(m,30,unrelated) == 30

def test_disabled_feed_preserves_existing_movement_and_battery_rules():
    m = moving()
    assert segment_speed(m,30,None) == 30
    assert segment_speed(m,30,{"source":"DISABLED"}) == 30
    m.state = "IDLE"
    assert segment_speed(m,30,feed("ROAD_CLOSED",None)) == 30
