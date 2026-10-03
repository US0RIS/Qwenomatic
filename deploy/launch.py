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
import signal
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



def protected_path(path):
    """Check lexical ancestors as well as the resolved symlink target."""
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        info = item.lstat()
        if info.st_uid != 0 or (not item.is_symlink() and info.st_mode & 0o022):
            raise SafetyError(f"unsafe runtime ownership or permissions: {item}")
        if item.is_symlink():
            protected_path(item.resolve(strict=True))


def verify_runtime(python):
    executable = Path(os.path.abspath(python))
    protected_path(executable)
    # Do not resolve bin/python before identifying its venv.
    venv = executable.parent.parent
    if (venv / "pyvenv.cfg").exists():
        for item in [venv, *venv.rglob("*")]:
            protected_path(item)
            if item.is_symlink() and item != executable and not item.resolve().is_relative_to(venv):
                if not (item.parent == executable.parent and item.name.startswith("python") and item.resolve() == executable.resolve()):
                    raise SafetyError(f"venv links must stay within the protected runtime tree: {item} -> {item.resolve()}")
        values = {key.strip(): value.strip() for key, value in (line.split("=", 1) for line in (venv / "pyvenv.cfg").read_text().splitlines() if "=" in line)}
        if values.get("include-system-site-packages", "false").strip().lower() == "true":
            raise SafetyError("venv must not include system site packages")
    else:
        raise SafetyError("farm Python must be an explicit protected virtual environment")


def operation_command(args, root):
    bootstrap = "import sys; sys.path.insert(0," + repr(root) + "); "
    if args.operation in {"run", "init", "verify"}:
        argv = ["qwenomatic", "--config-dir", args.config_dir, "--data-dir", args.data_dir, args.operation]
        if args.operation == "run":
            for flag, value in (("--ticks", args.ticks), ("--generations", args.generations)):
                if value is not None:
                    argv += [flag, str(value)]
        return bootstrap + "from supervisor.cli import main; sys.argv=" + repr(argv) + "; raise SystemExit(main())"
    if args.operation == 'improvement-campaign':
        if not args.feature:
            raise SafetyError('improvement-campaign needs --feature')
        argv = ['improvement_campaign', '--config-dir', args.config_dir, '--output', args.data_dir,
                '--feature', args.feature, '--generations', str(args.generations or 3)]
        if args.candidate:
            argv += ['--candidate', args.candidate]
        return bootstrap + "from scripts.improvement_campaign import main; sys.argv=" + repr(argv) + "; raise SystemExit(main())"
    scripts = {"evolution-ab": "evolution_ab", "campaign": "evolution_ab_campaign", "generation-zero": "generation_zero", "market-validity": "market_validity"}
    argv = [scripts[args.operation], "--config-dir", args.config_dir]
    argv += ["--data-dir" if args.operation == "generation-zero" else "--root", args.data_dir]
    if args.generations is not None:
        argv += ["--generations", str(args.generations)]
    if args.operation == "generation-zero" and getattr(args, "ticks", None) is not None:
        argv += ["--ticks", str(args.ticks)]
    if args.operation in ("campaign", "market-validity"):
        argv += ["--pairs", str(args.pairs)]
    return bootstrap + "from scripts." + scripts[args.operation] + " import main; sys.argv=" + repr(argv) + "; raise SystemExit(main())"


RECOVERY = Path("/run/qwenomatic-launch-state.json")


def cleanup(state):
    ns, host_if, table = state["namespace"], state["host_if"], state["table"]
    # Namespace identity, rather than reusable PID numbers, controls termination.
    result = subprocess.run(["ip", "netns", "pids", ns], text=True, capture_output=True)
    for pid in result.stdout.split():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    for command in (["ip", "netns", "del", ns], ["ip", "link", "del", host_if], ["nft", "delete", "table", "ip", table]):
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode and not any(word in result.stderr.lower() for word in ("no such", "cannot find", "does not exist")):
            raise SafetyError("cleanup failed: " + result.stderr[:1000])
    if state.get("evidence_path"):
        Path(state["evidence_path"]).unlink(missing_ok=True)
    Path("/proc/sys/net/ipv4/ip_forward").write_text(state["old_forward"])
    RECOVERY.unlink(missing_ok=True)


def recover():
    if not RECOVERY.exists():
        return
    state = protected_json(RECOVERY)
    import re
    suffix = state.get("suffix", "")
    if not re.fullmatch("[0-9a-f]{8}", suffix) or state.get("namespace") != "qwen-" + suffix or state.get("host_if") != "qh" + suffix or state.get("table") != "qwen_" + suffix or state.get("old_forward") not in ("0", "1"):
        raise SafetyError("invalid recovery record")
    evidence = state.get("evidence_path")
    if evidence is not None and not re.fullmatch(r"/run/qwenomatic-boundaries/[0-9]+\.json", evidence):
        raise SafetyError("invalid recovery evidence path")
    cleanup(state)


def save_recovery(state):
    temporary = RECOVERY.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(RECOVERY)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cleanup", action="store_true", help="recover an interrupted launch under the host lock")
    p.add_argument("--manifest")
    p.add_argument("--user")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--config-dir")
    p.add_argument("--data-dir")
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--ticks", type=int)
    p.add_argument("--generations", type=int)
    p.add_argument("--pairs", type=int, default=2)
    p.add_argument("--operation", choices=["run", "init", "verify", "evolution-ab", "campaign", "generation-zero", "improvement-campaign", "market-validity"], default="run")
    p.add_argument('--feature', choices=['knowledge','archive','crossover','small_model','autopilot','crowding','predictions','adaptation','red_team','fraud','self_tuning'])
    p.add_argument('--candidate', choices=['exploration','retirement','mutation','reserve'])
    args = p.parse_args()
    if os.geteuid() != 0:
        raise SafetyError("launcher requires root; farm does not")
    if not sys.flags.isolated or not sys.flags.no_site:
        raise SafetyError("start launcher with trusted system Python -I -S; site loading must be disabled before checks")
    lock = open("/run/qwenomatic-network-launch.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.cleanup:
        recover()
        return
    if RECOVERY.exists():
        raise SafetyError("interrupted launch: run --cleanup before starting")
    if not all((args.manifest, args.user, args.config_dir, args.data_dir)):
        p.error("manifest, user, config-dir and data-dir are required")
    verify_runtime(args.python)
    # Root will import trusted code and execute this Python. Protect both.
    for path in (Path(__file__).resolve().parents[1], Path(args.python).resolve()):
        for ancestor in (path, *path.parents):
            s = ancestor.stat()
            if s.st_uid != 0 or s.st_mode & 0o022:
                raise SafetyError("launcher code and Python must be root-owned, not group/world writable")
    checkout = Path(__file__).resolve().parents[1]
    for source in checkout.rglob("*"):
        if source.is_file() and source.suffix in {".py", ".yaml", ".sql"}:
            protected_path(source)
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
    if m["version"] == 1 and (m["adapters"] or m["model_url"] is not None):
        raise SafetyError("legacy direct-access manifests require broker migration")
    fingerprints = {}
    if m["version"] == 2:
        import hashlib
        for key in ("ca_file", "client_cert", "client_key"):
            file = Path(m["broker"][key])
            protected_path(file)
            if file.is_symlink():
                raise SafetyError("session files cannot be symlinks")
            fingerprints[key] = hashlib.sha256(file.read_bytes()).hexdigest()
    suffix = uuid.uuid4().hex[:8]
    ns, host_if, child_if, table = "qwen-" + suffix, "qh" + suffix, "qc" + suffix, "qwen_" + suffix
    # One /30 per simultaneous launch. IP address assignment fails on collisions.
    slot = int(suffix[:2], 16)
    host, child = f"10.203.{slot}.1", f"10.203.{slot}.2"
    evidence_path = None
    old_forward = Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()
    canary = socket.socket()
    state = {"suffix": suffix, "namespace": ns, "host_if": host_if, "table": table,
             "old_forward": old_forward, "evidence_path": None}
    save_recovery(state)  # durable before the first host mutation
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        run("ip", "netns", "add", ns)
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
        run("ip", "netns", "exec", ns, args.python, "-I", "-S", "-c", probe)
        # Reinstall the exact allowlist atomically, removing the temporary rule.
        run("ip", "netns", "exec", ns, "nft", "-f", "-",
            input="delete table inet qwenomatic\n" + rules(destinations, child))
        inode = run("ip", "netns", "exec", ns, args.python, "-I", "-S", "-c",
                    "import os; print(os.stat('/proc/self/ns/net').st_ino)").strip()
        directory = Path("/run/qwenomatic-boundaries")
        directory.mkdir(mode=0o755, exist_ok=True)
        if directory.stat().st_uid != 0 or directory.stat().st_mode & 0o022:
            raise SafetyError("unsafe boundary evidence directory")
        evidence_path = directory / (inode + ".json")
        state["evidence_path"] = str(evidence_path)
        save_recovery(state)
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
        command = operation_command(args, root)
        subprocess.run(prefix + [command], check=True, env=clean_env, cwd=root)
    finally:
        canary.close()
        cleanup(state)


if __name__ == "__main__":
    main()
