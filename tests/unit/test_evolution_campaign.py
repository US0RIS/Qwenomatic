from supervisor.experiments.evolution_ab import aggregate_reports


def _report(raw, did, baseline=0.0, equal=True):
    return {
        "baseline_balance": {"delta_net": baseline},
        "primary_effect": {
            "delta_net_realized": raw,
            "difference_in_differences_net_per_call": did,
            "all_generation_call_counts_equal": equal,
        },
    }


def test_campaign_aggregate_counts_signs_and_means():
    out = aggregate_reports([
        _report(10.0, 0.2, 1.0),
        _report(-4.0, -0.1, -2.0),
        _report(9.0, 0.3, 0.5),
    ])
    assert out["pairs"] == 3
    assert out["positive_raw_delta_pairs"] == 2
    assert out["positive_baseline_adjusted_pairs"] == 2
    assert out["mean_raw_delta_net"] == 5.0
    assert out["median_raw_delta_net"] == 9.0
    assert out["mean_difference_in_differences_net_per_call"] == 0.133333333
    assert out["all_call_counts_equal"] is True


def test_campaign_aggregate_propagates_call_count_failure():
    out = aggregate_reports([_report(1.0, 0.1), _report(1.0, 0.1, equal=False)])
    assert out["all_call_counts_equal"] is False
