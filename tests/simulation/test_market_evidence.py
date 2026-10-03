"""Full default 17/2/1 comparison. CI retains the actual ledgers as an artifact.

Uses the test-only simulated boundary, like the other simulation tests.
The separate kernel-safety CI job tests the real enforcement boundary.
"""
import os
from pathlib import Path

import pytest

from supervisor.experiments.market_validity import run_campaign, audit_ok


@pytest.mark.skipif(not os.environ.get('QWENOMATIC_MARKET_EVIDENCE_ROOT'),
                    reason='full default campaign is run by the market-evidence CI job')
def test_default_market_pair():
    root = Path(os.environ['QWENOMATIC_MARKET_EVIDENCE_ROOT'])
    report = run_campaign(None, root, seeds=(20261003,), generations=2, scenarios=('baseline',))
    assert report['real_world_validity'] == 'unestablished'
    assert report['simulation_screen'] == 'inconclusive_or_fails_assumptions'
    assert len(report['pairs']) == 1
    for name in ('treatment', 'control'):
        result = report['pairs'][0][name]
        assert result['role_counts'] == {'business': 17, 'research': 2, 'red_team': 1}
        assert audit_ok(result['audit'])
        assert result['tail']['pending'] == 0
        assert (root/result['ledger']).is_file()
        assert result['guardrails']['step_exceptions'] == 0
