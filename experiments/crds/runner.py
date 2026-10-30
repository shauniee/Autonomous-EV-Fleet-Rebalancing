"""Record an observed event stream; scenario drivers/cases are deferred to Phase 12.

Input NDJSON starts with a manifest (case_id, seed, policy, initial_state_hash,
workload_hash, transport, duration_seconds, zone_ids), followed by observation/decision/error events and a
final end event with completed=true. Observations contain zone desired/usable
stock and can retain full vehicle/dispatch snapshots. No future oracle is read.
"""
import argparse
import json

from .recording import EpisodeRecorder


def record_stream(events_path, output):
    recorder = None
    completed = False
    try:
        with open(events_path, encoding="utf-8") as events:
            first = json.loads(next(events))
            if first.pop("kind", None) != "manifest":
                raise ValueError("first event must be the episode manifest")
            recorder = EpisodeRecorder(output, first)
            ended = False
            for line in events:
                event = json.loads(line)
                if ended:
                    raise ValueError("events after episode end")
                kind = event.get("kind")
                if kind == "observation":
                    recorder.observe(event)
                elif kind == "decision":
                    recorder.decision(event)
                elif kind == "error":
                    recorder.error(event)
                elif kind == "end":
                    completed = event["completed"] is True
                    ended = True
                else:
                    raise ValueError("unknown episode event type")
    except Exception as exc:
        if recorder is not None:
            recorder.error({"kind": "error", "infrastructure": True, "error": str(exc)})
        raise
    finally:
        if recorder is not None:
            recorder.finish(completed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, help="Observed NDJSON events supplied by a case driver")
    parser.add_argument("--output", required=True, help="New episode output directory; existing paths are refused")
    args = parser.parse_args()
    record_stream(args.events, args.output)


if __name__ == "__main__":
    main()
