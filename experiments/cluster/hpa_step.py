"""Step-load the dashboard through the console's analytics API and poll HPA status."""
import json, sys, time, urllib.request
BASE = "http://127.0.0.1:8090/api/probe/v1/analytics"
out = open(sys.argv[1], "w")
def call(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body else "GET")
    return json.load(urllib.request.urlopen(req, timeout=10))
t0 = time.time()
for preset in ("NORMAL", "HIGH", "EXTREME"):
    call("/start", {"preset": preset, "duration_seconds": 120})
    end = time.time() + 125
    while time.time() < end:
        s = call("/status"); e = s["experiment"] or {}; i = s["infrastructure"]
        out.write(json.dumps({"t": round(time.time() - t0, 1), "preset": preset, "state": e.get("state"),
                              "rps": e.get("rps_observed"), "p95": e.get("p95_latency_ms"),
                              "errors": e.get("errors"), "attempted": e.get("attempted"),
                              "cpu": (i["hpa"] or {}).get("current_cpu_pct"),
                              "replicas": (i["pods"] or {}).get("total"),
                              "ready": (i["pods"] or {}).get("ready"),
                              "desired": (i["hpa"] or {}).get("desired_replicas")}) + "\n"); out.flush()
        time.sleep(3)
end = time.time() + 420  # watch scale-down after load stops
while time.time() < end:
    i = call("/status")["infrastructure"]
    out.write(json.dumps({"t": round(time.time() - t0, 1), "preset": "IDLE",
                          "cpu": (i["hpa"] or {}).get("current_cpu_pct"), "replicas": (i["pods"] or {}).get("total"),
                          "ready": (i["pods"] or {}).get("ready"), "desired": (i["hpa"] or {}).get("desired_replicas")}) + "\n"); out.flush()
    time.sleep(10)
