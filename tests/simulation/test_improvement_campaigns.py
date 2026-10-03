"""Paired mechanism verification under the existing empty unit-test boundary.

These receipts make no claim to kernel isolation, real inference or revenue.
"""
import json
import pytest
from supervisor.improvements import FEATURES
from supervisor.experiments.improvements import run_campaign


@pytest.mark.parametrize('feature', FEATURES)
def test_randomized_feature_campaign(tmp_path, feature):
    changes={'scheduler.exploration_share':.4} if feature=='self_tuning' else None
    report=run_campaign(tmp_path/feature,feature,seeds=(101,202),generations=2,changes=changes,
        overrides={'farm':{'generation':{'duration_hours':6,'tick_seconds':900}},'fitness':{'min_exposure_steps':5}})
    (tmp_path/'report.json').write_text(json.dumps(report,indent=2))
    assert report['status']=='simulation_verified' and not report['promoted']
    assert len(report['pairs'])==2
    for pair in report['pairs']:
        assert sorted(pair['order'])==['control','treatment']
        for arm in pair['arms'].values():
            assert arm['audit']['hash_chain']['ok'] and arm['audit']['accounting']['ok']
            assert all(arm['audit']['selection_replay'].values())
            assert arm['red_team_confirmed']==0
