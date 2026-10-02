import os
from pathlib import Path
from types import SimpleNamespace
import pytest
pytest.importorskip("fcntl")
from deploy.launch import verify_runtime, operation_command, recover, SafetyError


@pytest.mark.skipif(os.geteuid() != 0, reason="root ownership check")
def test_venv_checked_before_resolving_python():
    import tempfile
    with tempfile.TemporaryDirectory(dir="/run") as directory:
        root = Path(directory)
        (root / "bin").mkdir()
        (root / "lib/site-packages").mkdir(parents=True)
        (root / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        python = root / "bin/python"
        python.symlink_to('/usr/bin/python3')
        verify_runtime(python)
        packages = root / "lib/site-packages"
        packages.chmod(0o777)
        with pytest.raises(SafetyError, match="unsafe runtime"):
            verify_runtime(python)
        packages.chmod(0o755)
        (packages / "planted.pth").write_text("import os\n")
        (packages / "planted.pth").chmod(0o666)
        with pytest.raises(SafetyError, match="unsafe runtime"):
            verify_runtime(python)


@pytest.mark.parametrize("operation,module", [("evolution-ab", "evolution_ab"), ("campaign", "evolution_ab_campaign"), ("generation-zero", "generation_zero")])
def test_fixed_experiment_entrypoints(operation, module):
    args = SimpleNamespace(operation=operation, config_dir="/protected/config", data_dir="/farm/data", generations=3, pairs=4)
    command = operation_command(args, "/protected/code")
    assert "from scripts." + module + " import main" in command
    assert "--config-dir" in command and "/protected/config" in command
    assert "--generations" in command and "'3'" in command
    assert "/farm/data" in command
    if operation == "campaign":
        assert "--pairs" in command


def test_cli_generation_limit_forwarded():
    args = SimpleNamespace(operation="run", config_dir="/cfg", data_dir="/data", generations=2, ticks=1)
    command = operation_command(args, "/code")
    assert "--generations" in command and "--ticks" in command


def test_recovery_rejects_unrelated_resource_names(monkeypatch):
    monkeypatch.setattr("deploy.launch.RECOVERY", Path(__file__))
    monkeypatch.setattr("deploy.launch.protected_json", lambda _: {"suffix": "abcdef12", "namespace": "unrelated"})
    with pytest.raises(SafetyError, match="invalid recovery"):
        recover()
