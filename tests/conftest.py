import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture(autouse=True, scope="session")
def isolated_unit_boundary():
    """Unit tests inject an empty simulated boundary, never a production flag.

    Kernel isolation is exercised independently by deploy/kernel_smoke.py in
    Linux CI; these deterministic simulation tests need no network at all.
    Tests of NetworkBoundary itself use the original class directly.
    """
    class UnitBoundary:
        manifest = {"adapters": []}
        evidence = {}

        def __init__(self, config):
            pass

        def check(self):
            pass

        def record(self, store):
            pass

    patcher = pytest.MonkeyPatch()
    patcher.setattr("supervisor.safety.boundary.NetworkBoundary", UnitBoundary)
    yield
    patcher.undo()
