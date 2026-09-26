"""Shared simulation geography; no external routing or road claims."""
from math import asin, cos, radians, sin, sqrt


def distance_km(origin, destination):
    lat1, lat2 = radians(origin["latitude"]), radians(destination["latitude"])
    dlat = lat2 - lat1
    dlon = radians(destination["longitude"] - origin["longitude"])
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371.0088 * 2 * asin(sqrt(min(1, max(0, a))))


def predicted_soc(position, target, soc, capacity, consumption, route_factor, margin):
    distance = distance_km(position, target) * route_factor
    return soc - 100 * distance * consumption * margin / capacity
