#!/usr/bin/env python3
"""Root-only Linux acceptance test. No mocks and no external Internet dependency."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from storage.events import EventStore, EventType
    if os.geteuid() != 0:
        raise RuntimeError("kernel smoke test needs root")
    user = pwd.getpwnam("nobody")
    with tempfile.TemporaryDirectory(prefix="qwenomatic-smoke-", dir="/run") as tmp:
        base = Path(tmp)
        base.chmod(0o755)
        config = base / "config"
        shutil.copytree(root / "config", config)
        data = base / "data"
        data.mkdir()
        os.chown(data, user.pw_uid, user.pw_gid)
        manifest = base / "manifest.json"
        manifest.write_text(json.dumps({"version": 1, "operator": "kernel-smoke-operator",
                                        "approval_reference": "kernel-smoke-explicit-approval",
                                        "model_url": None, "adapters": []}))
        manifest.chmod(0o444)
        command = ["/usr/bin/python3", "-I", "-S", str(root / "deploy/launch.py"), "--manifest", str(manifest),
                   "--user", "nobody", "--python", args.python, "--config-dir", str(config),
                   "--data-dir", str(data), "--ticks", "2"]
        # The launcher proves a canary is reachable in the actual namespace
        # before removing its allow rule; production startup proves rejection.
        subprocess.run(command, check=True)
        store = EventStore(data / "ledger.sqlite3", read_only=True)
        try:
            verified = store.iter_events(types=[EventType.NETWORK_BARRIER_VERIFIED])
            access = store.iter_events(types=[EventType.ACCESS_APPROVED])
            assert verified and verified[-1].payload["result"] == "kernel_rejected"
            assert access and access[-1].author == "operator:kernel-smoke-operator"
            assert store.verify_chain() == (True, None)  # integrity of actual startup receipt
        finally:
            store.close()
        # Exercise each fixed experiment entry point with short generations.
        import yaml
        farm_path = config / "farm.yaml"
        farm = yaml.safe_load(farm_path.read_text())
        farm["generation"]["duration_hours"] = 2 / 3600
        farm["generation"]["tick_seconds"] = 1
        farm_path.write_text(yaml.safe_dump(farm))
        for operation, generations in (("generation-zero", "1"), ("evolution-ab", "2"), ("campaign", "2")):
            experiment_data = base / operation
            experiment_data.mkdir()
            os.chown(experiment_data, user.pw_uid, user.pw_gid)
            experiment = command[:-2]
            experiment[experiment.index("--data-dir") + 1] = str(experiment_data)
            subprocess.run(experiment + ["--operation", operation, "--generations", generations, "--pairs", "1"], check=True)
        # A writable package tree must be rejected before .pth execution.
        site = Path(args.python).parent.parent / "lib"
        planted = next(site.glob("python*/site-packages")) / "qwenomatic-smoke.pth"
        marker = base / "PTH_EXECUTED"
        planted.write_text("import pathlib; pathlib.Path(" + repr(str(marker)) + ").touch()\n")
        planted.chmod(0o666)
        try:
            rejected = subprocess.run(command, capture_output=True, text=True)
            assert rejected.returncode != 0 and "unsafe runtime" in rejected.stderr
            assert not marker.exists()
        finally:
            planted.unlink()
        # Kill the launcher after mutation; children and host state survive
        # until the independent recovery command removes them.
        old_forward = Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()
        interrupted = subprocess.Popen(command[:-2] + ["--ticks", "1000000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        recovery = Path("/run/qwenomatic-launch-state.json")
        deadline = time.monotonic() + 60
        try:
            while time.monotonic() < deadline:
                if recovery.exists():
                    state = json.loads(recovery.read_text())
                    if state.get("evidence_path") and Path(state["evidence_path"]).exists():
                        break
                if interrupted.poll() is not None:
                    raise AssertionError("launcher exited before interruption test")
                time.sleep(0.05)
            else:
                raise AssertionError("launcher never produced recovery evidence")
        finally:
            interrupted.kill()
            interrupted.wait()
        cleanup = ["/usr/bin/python3", "-I", "-S", str(root / "deploy/launch.py"), "--cleanup"]
        subprocess.run(cleanup, check=True)
        subprocess.run(cleanup, check=True)  # idempotent
        assert not recovery.exists() and not Path(state["evidence_path"]).exists()
        assert Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() == old_forward
        assert state["namespace"] not in subprocess.check_output(["ip", "netns", "list"], text=True)
        assert state["table"] not in subprocess.check_output(["nft", "list", "ruleset"], text=True)
        # Direct execution outside the namespace must refuse to initialize.
        code = "import sys;sys.path.insert(0," + repr(str(root)) + ");from supervisor.cli import main;raise SystemExit(main())"
        direct = subprocess.run(
            ["setpriv", "--reuid", str(user.pw_uid), "--regid", str(user.pw_gid), "--clear-groups",
             "--bounding-set=-all", "--no-new-privs", args.python, "-I", "-c", code,
             "--config-dir", str(config), "--data-dir", str(data), "run", "--ticks", "1"],
            capture_output=True, text=True)
        assert direct.returncode == 2 and "boundary" in direct.stderr
        # A writable manifest must be refused before creating a namespace.
        manifest.chmod(0o666)
        invalid = subprocess.run(command, capture_output=True, text=True)
        assert invalid.returncode != 0 and "immutable" in invalid.stderr
    print("PASS: live positive control, kernel outbound rejection, actual farm startup, ledger receipt, unsafe startup refusal, protected experiments, writable venv refusal, SIGKILL recovery")


if __name__ == "__main__":
    main()
