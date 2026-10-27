"""Compare authoritative and Redis-backed DISPLAY reads; development only.

Runs paired, interleaved requests. Not a controlled performance study.
Caches never participate in ranking, eligibility, allocation or dispatch.
"""
import argparse
from statistics import median
from time import perf_counter
from urllib.request import urlopen


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))]


def fetch(url):
    start = perf_counter()
    with urlopen(url, timeout=5) as response:
        response.read()
        cache = response.headers.get("X-Dashboard-Cache", "AUTHORITATIVE")
    return (perf_counter() - start) * 1000, cache


def measure(base, original, cached, samples):
    data = {"authoritative": [], "dashboard": []}
    statuses = {}
    for index in range(samples):
        # Alternate the order to limit simple systematic order bias.
        pair = [("authoritative", original), ("dashboard", cached)]
        if index % 2:
            pair.reverse()
        for name, path in pair:
            duration, status = fetch(base + path)
            data[name].append(duration)
            if name == "dashboard":
                statuses[status] = statuses.get(status, 0) + 1
    def numbers(label):
        values = data[label]
        return f"p50={median(values):.2f}ms p95={percentile(values, .95):.2f}ms"
    print(f"{original}: original {numbers('authoritative')} | cached {numbers('dashboard')} | {statuses}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--requests", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.requests <= 2000:
        parser.error("requests must be between 1 and 2000")
    base = args.base_url.rstrip("/")
    measure(base, "/api/demand/heat-map", "/api/demand/dashboard/heat-map", args.requests)
    measure(base, "/api/facilities/facilities", "/api/facilities/dashboard/facilities", args.requests)
    measure(base, "/api/fleet/vehicles", "/api/fleet/dashboard/vehicles", args.requests)
    print("Smoke timing only: host load, network variance, Fleet updates and cache warmness may affect results.")


if __name__ == "__main__":
    main()
