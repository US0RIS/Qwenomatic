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
        command = [args.python, str(root / "deploy/launch.py"), "--manifest", str(manifest),
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
    print("PASS: live positive control, kernel outbound rejection, actual farm startup, ledger receipt, unsafe startup refusal")


if __name__ == "__main__":
    main()
