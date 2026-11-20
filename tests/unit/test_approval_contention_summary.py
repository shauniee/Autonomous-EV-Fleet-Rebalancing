from experiments.cluster.approval_contention import summarise


def op(approval, ms, code=None):
    return {"approval": approval, "approve_ms": ms, "code": code}


def test_summary_counts_winners_double_claims_leaks_and_latency():
    rows = [
        {"operators": 2, "winners": 1, "claims_match_winners": True, "leaked_losers": [], "makespan_ms": 50.0,
         "ops": [op("APPROVED", 30.0), op(None, 50.0, "VEHICLE_NOT_ELIGIBLE:BUSY")]},
        {"operators": 2, "winners": 2, "claims_match_winners": False, "leaked_losers": ["r"], "makespan_ms": 70.0,
         "ops": [op("APPROVED", 40.0), op("APPROVED", 70.0), {"skipped": True}]},
    ]
    s = summarise(rows)[2]
    assert (s["rounds"], s["one_winner_rounds"], s["double_claims"], s["leaked_losers"]) == (2, 1, 1, 1)
    assert s["winner_ms_median"] == 40.0 and s["loser_ms_median"] == 50.0 and s["approve_ms_p95"] == 70.0
    assert s["loser_codes"] == ["VEHICLE_NOT_ELIGIBLE:BUSY"]
