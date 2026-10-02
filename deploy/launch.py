#!/usr/bin/env python3
"""Privileged operator launcher. Run from a root-owned checkout.

Creates a per-run netns, installs fail-closed nftables, proves canary rejection,
then drops all capabilities and uid before executing the supervisor.
Never exposed as an agent tool.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from supervisor.safety.boundary import protected_json, validate_manifest, SafetyError
from storage.events import digest


def run(*args, input=None):
    try:
        return subprocess.run(args, input=input, text=True, check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as exc:
        raise SafetyError(f"network setup command {args[0]} failed: {exc.stderr[:2000]}") from exc


def rules(destinations, child):
    allows = "\n".join(f"ip daddr {ip} tcp dport {port} accept" for ip, port in sorted(destinations))
    return f"""table inet qwenomatic {{
      chain output {{
        type filter hook output priority -150; policy drop;
        {allows}
        ip daddr {child} icmp type destination-unreachable icmp code admin-prohibited accept
        reject with icmpx type admin-prohibited
      }}
      chain input {{
        type filter hook input priority -150; policy drop;
        ip daddr {child} icmp type destination-unreachable icmp code admin-prohibited accept
        ct state established,related accept
      }}
      chain forward {{
        type filter hook forward priority -150; policy drop;
      }}
    }}
    """


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--config-dir", required=True)
    p.add_argument("--data-dir", required=True)
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--ticks", type=int)
    p.add_argument("--operation", choices=["run", "init", "verify"], default="run")
    args = p.parse_args()
    if os.geteuid() != 0:
        raise SafetyError("launcher requires root; farm does not")
    lock = open("/run/qwenomatic-network-launch.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Root will import trusted code and execute this Python. Protect both.
    for path in (Path(__file__).resolve().parents[1], Path(args.python).resolve()):
        for ancestor in (path, *path.parents):
            s = ancestor.stat()
            if s.st_uid != 0 or s.st_mode & 0o022:
                raise SafetyError("launcher code and Python must be root-owned, not group/world writable")
    checkout = Path(__file__).resolve().parents[1]
    for source in checkout.rglob("*"):
        if source.is_file() and source.suffix in {".py", ".yaml", ".sql"}:
            s = source.lstat()
            if s.st_uid != 0 or s.st_mode & 0o022 or source.is_symlink():
                raise SafetyError("trusted code and config cannot be writable by the farm")
    for source in (Path(args.config_dir) / name for name in ("farm.yaml", "policy.yaml", "fitness.yaml")):
        for ancestor in (source, *source.parents):
            s = ancestor.lstat()
            if s.st_uid != 0 or s.st_mode & 0o022 or ancestor.is_symlink():
                raise SafetyError("operator farm config must be protected")
    manifest_path = Path(args.manifest)
    m = protected_json(manifest_path)
    destinations = validate_manifest(m)
    user = pwd.getpwnam(args.user)
    if not user.pw_uid:
        raise SafetyError("farm uid must be non-root")
    fingerprints = {}
    for a in m["adapters"]:
        secret = protected_json(Path(a["credential_file"]))
        if set(secret) != {"token"} or not isinstance(secret["token"], str) or not secret["token"]:
            raise SafetyError("credential file must contain only a nonempty token")
        fingerprints[a["name"]] = digest(secret)
    suffix = uuid.uuid4().hex[:8]
    ns, host_if, child_if, table = "qwen-" + suffix, "qh" + suffix, "qc" + suffix, "qwen_" + suffix
    # One /30 per simultaneous launch. IP address assignment fails on collisions.
    slot = int(suffix[:2], 16)
    host, child = f"10.203.{slot}.1", f"10.203.{slot}.2"
    evidence_path = None
    old_forward = Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()
    canary = socket.socket()
    created = False
    try:
        run("ip", "netns", "add", ns)
        created = True
        run("ip", "link", "add", host_if, "type", "veth", "peer", "name", child_if)
        run("ip", "link", "set", child_if, "netns", ns)
        run("ip", "addr", "add", host + "/30", "dev", host_if)
        run("ip", "link", "set", host_if, "up")
        run("ip", "netns", "exec", ns, "ip", "addr", "add", child + "/30", "dev", child_if)
        run("ip", "netns", "exec", ns, "ip", "link", "set", child_if, "up")
        run("ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up")
        run("ip", "netns", "exec", ns, "ip", "route", "add", "default", "via", host)
        # IPv6, UDP, raw IP, DNS, loopback side channels: no allow rules.
        run("ip", "netns", "exec", ns, "nft", "-f", "-", input=rules(destinations, child))
        actual = run("ip", "netns", "exec", ns, "nft", "list", "ruleset")
        if "policy drop" not in actual or "admin-prohibited" not in actual:
            raise SafetyError("installed firewall could not be confirmed")
        Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
        run("nft", "-f", "-", input=f"""table ip {table} {{
          chain nat {{
            type nat hook postrouting priority srcnat; policy accept;
            ip saddr {child} masquerade
          }}
        }}""")
        canary.bind((host, 0))
        canary.listen(16)
        port = canary.getsockname()[1]
        if (host, port) in destinations:
            raise SafetyError("canary collides with allowlist")
        def accept():
            while True:
                try:
                    conn, _ = canary.accept()
                    conn.close()
                except OSError:
                    return
        threading.Thread(target=accept, daemon=True).start()
        # Positive control on the same route, from the same namespace, BEFORE
        # canary policy: temporarily allow this one endpoint, prove live service.
        run("ip", "netns", "exec", ns, "nft", "insert", "rule", "inet", "qwenomatic", "output",
            "ip", "daddr", host, "tcp", "dport", str(port), "accept")
        probe = "import socket; s=socket.create_connection((" + repr(host) + "," + str(port) + "),2); s.close()"
        run("ip", "netns", "exec", ns, args.python, "-I", "-c", probe)
        # Reinstall the exact allowlist atomically, removing the temporary rule.
        run("ip", "netns", "exec", ns, "nft", "-f", "-",
            input="delete table inet qwenomatic\n" + rules(destinations, child))
        inode = run("ip", "netns", "exec", ns, args.python, "-I", "-c",
                    "import os; print(os.stat('/proc/self/ns/net').st_ino)").strip()
        directory = Path("/run/qwenomatic-boundaries")
        directory.mkdir(mode=0o755, exist_ok=True)
        if directory.stat().st_uid != 0 or directory.stat().st_mode & 0o022:
            raise SafetyError("unsafe boundary evidence directory")
        evidence_path = directory / (inode + ".json")
        evidence = {"namespace": "net:[" + inode + "]", "uid": user.pw_uid,
                    "manifest_path": str(manifest_path), "manifest_digest": digest(m),
                    "destinations": sorted([list(d) for d in destinations]),
                    "credential_fingerprints": fingerprints, "canary": [host, port]}
        with evidence_path.open("x") as f:
            json.dump(evidence, f)
        evidence_path.chmod(0o444)
        # setpriv is the last privileged step. Python uses isolated mode and
        # imports only the root-owned checkout, not an agent workspace.
        prefix = ["ip", "netns", "exec", ns, "setpriv", "--reuid", str(user.pw_uid),
                  "--regid", str(user.pw_gid), "--clear-groups", "--bounding-set=-all",
                  "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs",
                  args.python, "-I", "-c"]
        root = str(Path(__file__).resolve().parents[1])
        bootstrap = "import sys; sys.path.insert(0," + repr(root) + "); "
        cfg = "from supervisor.config import FarmConfig; cfg=FarmConfig.load(" + repr(args.config_dir) + ",data_dir=" + repr(args.data_dir) + "); "
        check = bootstrap + cfg + "from supervisor.safety.boundary import NetworkBoundary; NetworkBoundary(cfg).check(); print('kernel barrier verified')"
        clean_env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
        subprocess.run(prefix + [check], check=True, env=clean_env, cwd=root)
        if args.check_only:
            return
        command = bootstrap + "from supervisor.cli import main; sys.argv=" + repr(
            ["qwenomatic", "--config-dir", args.config_dir, "--data-dir", args.data_dir, args.operation]
            + (["--ticks", str(args.ticks)] if args.ticks is not None and args.operation == "run" else [])) + "; raise SystemExit(main())"
        subprocess.run(prefix + [command], check=True, env=clean_env, cwd=root)
    finally:
        canary.close()
        if evidence_path:
            evidence_path.unlink(missing_ok=True)
        if created:
            # No child survives teardown, even when interrupted.
            pids = run("ip", "netns", "pids", ns).split()
            for pid in pids:
                try:
                    os.kill(int(pid), 9)
                except ProcessLookupError:
                    pass
            subprocess.run(["ip", "netns", "del", ns], capture_output=True)
            subprocess.run(["ip", "link", "del", host_if], capture_output=True)
        subprocess.run(["nft", "delete", "table", "ip", table], capture_output=True)
        Path("/proc/sys/net/ipv4/ip_forward").write_text(old_forward)


if __name__ == "__main__":
    main()
