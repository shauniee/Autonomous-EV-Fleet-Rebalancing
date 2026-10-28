"""Bounded, read-only LTA traffic feeds and deterministic route risk assessment.

Speed bands are point-to-road *observations*, not a drivable road graph.
Approved works carry road names, not precise closure geometries. Only explicit
geolocated road-block/diversion incidents cause a hard route exclusion.
"""
import asyncio
import os
from datetime import date, datetime, timezone
from math import cos, radians, isfinite
import httpx

BASE = "https://datamall2.mytransport.sg/ltaodataservice"
FEEDS = {
    "speeds": ("v3/TrafficSpeedBands", 300, 900),
    "incidents": ("TrafficIncidents", 90, 300),
    "roadworks": ("RoadWorks", 86400, 172800),
}
MAX_PAGES = 20
PAGE_SIZE = 500


class TrafficDataError(Exception):
    pass


def _as_float(value):
    x = float(value)
    if not isfinite(x):
        raise ValueError("nonfinite measurement")
    return x


def _point(lat, lon):
    latitude, longitude = _as_float(lat), _as_float(lon)
    if not (1.1 <= latitude <= 1.5 and 103.5 <= longitude <= 104.1):
        raise ValueError("outside Singapore traffic coverage")
    return {"latitude": latitude, "longitude": longitude}


def normalize(name, rows):
    result = []
    for row in rows:
        try:
            if name == "speeds":
                start = _point(row["StartLat"], row["StartLon"])
                end = _point(row["EndLat"], row["EndLon"])
                minimum, maximum = _as_float(row["MinimumSpeed"]), _as_float(row["MaximumSpeed"])
                if not 0 <= minimum <= maximum <= 150:
                    continue
                result.append({"id": str(row["LinkID"]), "road": str(row.get("RoadName", "")).upper().strip(),
                               "start": start, "end": end, "min_kph": minimum, "max_kph": maximum})
            elif name == "incidents":
                pt = _point(row["Latitude"], row["Longitude"])
                kind = str(row.get("Type", "")).upper().strip()
                result.append({"id": str(row.get("IncidentID", len(result))),
                               "type": kind, "position": pt,
                               "message": str(row.get("Message", ""))[:240]})
            else:
                start, end = date.fromisoformat(str(row["StartDate"])[:10]), date.fromisoformat(str(row["EndDate"])[:10])
                road = str(row.get("RoadName", "")).upper().strip()
                if not road or end < start:
                    continue
                result.append({"id": str(row.get("EventID", len(result))), "road": road,
                               "start_date": start.isoformat(), "end_date": end.isoformat()})
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return result


class TrafficMonitor:
    def __init__(self, client, source=None, account_key=None, clock=None):
        self.client = client
        self.source = (source or os.getenv("TRAFFIC_SOURCE", "DISABLED")).upper()
        self.key = account_key if account_key is not None else os.getenv("LTA_DATAMALL_ACCOUNT_KEY", "")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = asyncio.Lock()
        self.cache = {}

    async def _fetch(self, name):
        path, _, _ = FEEDS[name]
        rows = []
        previous = None
        for offset in range(0, MAX_PAGES * PAGE_SIZE, PAGE_SIZE):
            try:
                response = await self.client.get(f"{BASE}/{path}", params={"$skip": offset},
                    headers={"AccountKey": self.key, "Accept": "application/json"},
                    timeout=8, follow_redirects=False)
                if response.status_code in (401, 403):
                    raise TrafficDataError("AUTH_FAILED")
                if response.status_code == 429:
                    raise TrafficDataError("RATE_LIMITED")
                response.raise_for_status()
                payload = response.json()
                page = payload.get("value")
                if not isinstance(page, list) or len(page) > PAGE_SIZE or any(not isinstance(r, dict) for r in page):
                    raise TrafficDataError("INVALID_RESPONSE")
                if page and page == previous:
                    raise TrafficDataError("INVALID_PAGINATION")
                rows.extend(page)
                if len(page) < PAGE_SIZE:
                    return normalize(name, rows)
                previous = page
            except (httpx.HTTPError, ValueError, TypeError):
                raise TrafficDataError("FETCH_FAILED") from None
        raise TrafficDataError("PAGINATION_LIMIT")

    def cached_snapshot(self):
        """Non-blocking read: never fetch or wait for a provider on a hot path."""
        now = self.clock()
        if self.source != "LTA_DATAMALL":
            return {"source": "DISABLED", "generated_at": now.isoformat(),
                    "status": "DISABLED", "feeds": {}, "speeds": [], "incidents": [], "roadworks": []}
        if not self.key:
            return {"source": "LTA_DATAMALL", "generated_at": now.isoformat(),
                    "status": "MISSING_KEY", "feeds": {}, "speeds": [], "incidents": [], "roadworks": []}
        feeds, data = {}, {}
        for name, (_, _, max_age) in FEEDS.items():
            entry = self.cache.get(name)
            observed = entry["observed_at"] if entry else None
            age = (now - observed).total_seconds() if observed else None
            fresh = age is not None and 0 <= age < max_age
            feeds[name] = {
                "status": "FRESH" if fresh else "STALE" if age is not None else
                          entry["status"] if entry else "PENDING",
                "observed_at": observed.isoformat() if observed else None,
                "count": len(entry["rows"]) if fresh else 0,
                "error": entry["status"] if entry and entry["status"] != "FRESH" else None,
            }
            data[name] = entry["rows"] if entry and fresh else []
        return {"source": "LTA_DATAMALL", "generated_at": now.isoformat(),
                "status": "FRESH" if all(f["status"] == "FRESH" for f in feeds.values()) else "PARTIAL",
                "feeds": feeds, **data}

    async def refresh_loop(self):
        """Refresh independently of route ranking, dashboards and approvals."""
        while True:
            try:
                await asyncio.wait_for(self.snapshot(), timeout=75)
            except (asyncio.TimeoutError, TrafficDataError):
                pass  # Unknown status remains visible through cached_snapshot.
            await asyncio.sleep(30)

    async def snapshot(self):
        now = self.clock()
        if self.source != "LTA_DATAMALL":
            return {"source": "DISABLED", "generated_at": now.isoformat(),
                    "status": "DISABLED", "feeds": {}, "speeds": [], "incidents": [], "roadworks": []}
        if not self.key:
            return {"source": "LTA_DATAMALL", "generated_at": now.isoformat(),
                    "status": "MISSING_KEY", "feeds": {}, "speeds": [], "incidents": [], "roadworks": []}
        async with self.lock:
            feeds, data = {}, {}
            for name, (_, refresh, max_age) in FEEDS.items():
                entry = self.cache.get(name)
                if entry is None or (now - entry["attempted_at"]).total_seconds() >= refresh:
                    try:
                        items = await self._fetch(name)
                        entry = {"attempted_at": now, "observed_at": now,
                                 "status": "FRESH", "rows": items}
                    except TrafficDataError as exc:
                        if entry is None:
                            entry = {"attempted_at": now, "observed_at": None,
                                     "status": str(exc), "rows": []}
                        else:
                            entry = {**entry, "attempted_at": now, "status": str(exc)}
                    self.cache[name] = entry
                age = (now - entry["observed_at"]).total_seconds() if entry["observed_at"] else None
                fresh = age is not None and 0 <= age < max_age
                data[name] = entry["rows"] if fresh else []
                feeds[name] = {"status": "FRESH" if fresh else "STALE" if age is not None else entry["status"],
                               "observed_at": entry["observed_at"].isoformat() if entry["observed_at"] else None,
                               "count": len(data[name]), "error": entry["status"] if entry["status"] != "FRESH" else None}
            status = "FRESH" if all(f["status"] == "FRESH" for f in feeds.values()) else "PARTIAL"
            return {"source": "LTA_DATAMALL", "generated_at": now.isoformat(),
                    "status": status, "feeds": feeds, **data}


def _xy(p):
    return (p["longitude"] * 111.195 * cos(radians(p["latitude"])), p["latitude"] * 111.195)


def _segment_distance(point, a, b):
    # Local Singapore tangent-plane approximation; metres-scale screening only.
    x, y = _xy(point)
    ax, ay = _xy(a)
    bx, by = _xy(b)
    dx, dy = bx - ax, by - ay
    u = max(0, min(1, ((x-ax)*dx + (y-ay)*dy) / (dx*dx + dy*dy))) if dx*dx + dy*dy > 0 else 0
    return ((x - (ax + u*dx))**2 + (y - (ay + u*dy))**2)**.5


def _near_route(point, geometry, threshold_km=.12):
    return any(_segment_distance(point, a, b) <= threshold_km for a, b in zip(geometry, geometry[1:]))


def assess_traffic(geometry, distance_km, duration_seconds, traffic, when=None):
    """Pure scoring: assessed routes only, unknown feed != clear road."""
    when = when or datetime.now(timezone.utc)
    active = traffic.get("source") == "LTA_DATAMALL"
    if not active or len(geometry) < 2:
        return {"status": "UNKNOWN", "blocked": False, "delay_factor": None,
                "estimated_duration_seconds": None, "risk": None,
                "matched_speed_segments": 0, "roadworks": 0, "incidents": 0}
    matched = [row for row in traffic.get("speeds", []) if
               _near_route(row["start"], geometry, .10) or _near_route(row["end"], geometry, .10)]
    road_names = {item["road"] for item in matched if item["road"]}
    current = when.date().isoformat()
    roadworks = [w for w in traffic.get("roadworks", [])
                 if w["road"] in road_names and w["start_date"] <= current <= w["end_date"]]
    incidents = [i for i in traffic.get("incidents", [])
                 if _near_route(i["position"], geometry, .12)]
    blocked = any(x["type"] in ("ROAD BLOCK", "DIVERSION") for x in incidents)
    # Speed observations cover only a subset of Singapore roads. Use the
    # unmodified routing ETA as baseline for uncovered stretches; never claim
    # to reconstruct per-edge travel times from these sparse observations.
    speeds = [(r["min_kph"]+r["max_kph"])/2 for r in matched]
    congestion = max(1.0, min(4.0, 45 / max(8, sum(speeds) / len(speeds)))) if speeds else 1.0
    delay = min(5, congestion * (1 + min(len(roadworks), 3)*.12 +
                                 sum(.20 for i in incidents if i["type"] not in ("ROAD BLOCK", "DIVERSION"))))
    risks = min(1, .25*len(roadworks) + .4*len(incidents))
    status = "FRESH" if traffic.get("status") == "FRESH" and matched else "PARTIAL"
    return {"status": status, "blocked": blocked, "delay_factor": round(delay, 3),
            "estimated_duration_seconds": round(duration_seconds*delay, 1),
            "risk": risks, "matched_speed_segments": len(matched),
            "roadworks": len(roadworks), "incidents": len(incidents)}


def rank_cost(assessment, energy_kwh, origin_supply_penalty=0.0):
    if assessment["blocked"]:
        return None
    if assessment["estimated_duration_seconds"] is None:
        return None
    t = min(1, assessment["estimated_duration_seconds"]/3600)
    e = min(1, energy_kwh/15)
    return round(.45*t + .25*e + .20*(assessment["risk"] or 0) +
                 .10*min(1,max(0,origin_supply_penalty)), 5)
