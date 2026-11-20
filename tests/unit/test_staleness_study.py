from experiments.crds.staleness_study import LAGS, run, summarise


def test_fresh_data_needs_no_recheck_and_stale_rechecks_never_fail_claims():
    records = run(15, 3, samples=5)
    fresh = [r for r in records if r["lag_s"] == 0]
    assert fresh and all(r["crds_chosen"] == r["crds_recheck_chosen"] for r in fresh)
    assert not any(r["crds_recheck_claim_failed"] for r in records)
    summary = summarise(records, 3)
    assert set(summary) == {str(lag) for lag in LAGS}
    for block in summary.values():
        for row in block["policies"].values():
            assert 0 <= row["harm_share"] <= 1 and row["mean_cvar10"] <= row["mean_gain"]
