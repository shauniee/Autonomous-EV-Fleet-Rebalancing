import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.phase4.analysis import analyze


def main():
    parser = argparse.ArgumentParser(description="Regenerate Phase 4 tables and graphs from raw JSONL")
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    result = analyze(args.run / "raw" / "trials.jsonl", args.run)
    print(json.dumps(result, indent=2))
    return 0 if not result["infrastructure_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
