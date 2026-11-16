from experiments.crds.decision_study import run, summarise


def test_out_of_sample_check_is_reported_without_changing_declared_results():
    plain = run(20, 7)
    sampled = run(20, 7, samples=5)
    declared_keys = [k for k in plain[0] if "_oos_" not in k]
    assert [{k: r[k] for k in declared_keys} for r in sampled] == plain
    oos = summarise(sampled, 7)["out_of_sample"]
    for policy in ("nearest", "weighted", "crds_nominal", "crds"):
        assert 0 <= oos[policy]["harm_share"] <= 1
        assert oos[policy]["mean_cvar10"] <= oos[policy]["mean_gain"]
