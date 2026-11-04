import argparse
import asyncio
import logging
import os
from ev_fleet.contracts.common import Position
from ev_fleet.demand.store import SINGAPORE_ZONES
from .node import run_node


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Singapore EV simulator; stationary unless --dispatch-url is set")
    parser.add_argument("--fleet-url", default=os.getenv("FLEET_URL", "http://localhost:8001"))
    parser.add_argument("--vehicle-id", default="ev-01")
    parser.add_argument("--zone", choices=[zone[0] for zone in SINGAPORE_ZONES], default="bishan")
    parser.add_argument("--soc", type=float, default=80)
    parser.add_argument("--interval", type=float, default=float(os.getenv("TELEMETRY_INTERVAL_SECONDS", "5")))
    parser.add_argument("--updates", type=int, help="Exit after this many accepted observations")
    parser.add_argument("--dispatch-url", help="Enable relocation commands from this Dispatch Service")
    parser.add_argument("--facilities-url", default=os.getenv("FACILITIES_URL"), help="Enable allocated parking and simulated charging")
    parser.add_argument("--facilities-grace-seconds", type=float,
                        default=float(os.getenv("FACILITIES_FAILURE_GRACE_SECONDS", "10")),
                        help="Retain the last verified Facilities state through brief refresh failures")
    parser.add_argument("--speed-kph", type=float, default=30)
    parser.add_argument("--time-scale", type=float, default=float(os.getenv("SIMULATION_TIME_SCALE", "10")), help="Simulated seconds per real second (demo default 10x)")
    parser.add_argument("--tick-seconds", type=float, default=.2)
    parser.add_argument("--poll-interval", type=float, default=1)
    args = parser.parse_args(argv)
    if not 0 <= args.soc <= 100 or args.interval <= 0 or (args.updates is not None and args.updates < 1):
        parser.error("SOC must be 0..100; interval and updates must be positive")
    import math
    values = [args.speed_kph, args.time_scale, args.tick_seconds, args.poll_interval, args.interval]
    if any(not math.isfinite(value) or value <= 0 for value in values):
        parser.error("simulation speeds and intervals must be finite and positive")
    if not math.isfinite(args.facilities_grace_seconds) or args.facilities_grace_seconds < 0:
        parser.error("facilities grace must be finite and non-negative")
    if args.dispatch_url and args.updates:
        parser.error("--updates is only supported in stationary mode")
    return args


def main():
    args = parse_args()
    zone = next(zone for zone in SINGAPORE_ZONES if zone[0] == args.zone)
    position = Position(latitude=zone[2], longitude=zone[3]).model_dump()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_node(args.fleet_url, args.vehicle_id, position, args.soc, args.interval, args.updates,
                        dispatch_url=args.dispatch_url, speed_kph=args.speed_kph, time_scale=args.time_scale,
                        tick_seconds=args.tick_seconds, poll_interval=args.poll_interval,
                        facilities_url=args.facilities_url,
                        facilities_grace_seconds=args.facilities_grace_seconds))


if __name__ == "__main__":
    main()
