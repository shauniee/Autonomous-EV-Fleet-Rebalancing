import csv
import pytest
from experiments.strategy_evaluation.analysis import bootstrap_mean_ci, describe, percentile, wilson, write_csv


def test_descriptive_statistics_and_percentile():
    summary = describe([1, 2, 3, 4], bootstrap_samples=100, seed=1)
    assert summary["mean"] == 2.5 and summary["median"] == 2.5
    assert summary["stddev"] == pytest.approx(1.2909944487)
    assert summary["p95"] == pytest.approx(3.85)
    assert summary["ci95_low"] <= summary["mean"] <= summary["ci95_high"]
    assert percentile([], .95) is None


def test_confidence_intervals_are_deterministic_and_bounded():
    assert bootstrap_mean_ci([1, 2, 3], samples=100, seed=4) == bootstrap_mean_ci([1, 2, 3], samples=100, seed=4)
    low, high = wilson(8, 10)
    assert 0 <= low < .8 < high <= 1
    assert wilson(0, 0) == (None, None)


def test_csv_writer_accepts_union_of_summary_columns(tmp_path):
    path = tmp_path / "summary.csv"
    write_csv(path, [{"metric": "distance", "mean": 4},
                     {"metric": "failure_rate", "mean": .2, "count": 2}])
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["count"] == "" and rows[1]["count"] == "2"
