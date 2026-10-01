from supervisor.experiments.evolution_ab import compare, run_arm


def test_ab_baseline_is_matched_and_control_stays_frozen(tmp_path):
    treatment = run_arm(
        arm="treatment", data_dir=tmp_path / "treatment", generations=2,
        backend="simulated", progress_every_ticks=0,
    )
    control = run_arm(
        arm="control", data_dir=tmp_path / "control", generations=2,
        backend="simulated", progress_every_ticks=0,
    )

    # Evolution occurs only after Generation 0, so the deterministic simulated
    # baseline must match exactly between arms.
    t0, c0 = treatment.generations[0], control.generations[0]
    assert t0.steps == c0.steps
    assert t0.net_realized == c0.net_realized
    assert t0.gross_revenue == c0.gross_revenue

    # The control never retires/reproduces; the treatment does.
    assert all(g.retired == 0 and g.offspring == 0 and g.immigrants == 0 for g in control.generations)
    assert treatment.generations[0].retired > 0
    assert treatment.generations[0].offspring > 0

    report = compare(treatment, control)
    assert report["primary_effect"]["all_generation_call_counts_equal"]
    assert report["design"]["baseline_generation"] == 0
