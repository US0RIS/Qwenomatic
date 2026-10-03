"""Verify root-owned namespace evidence and prove rejection using a live canary.

No environment flag, YAML switch, or model output can disable this check.
The privileged launcher creates the evidence after installing nftables rules.
"""
from __future__ import annotations

import errno
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import struct
from urllib.parse import urlsplit

from storage.events import EventType, digest


class SafetyError(RuntimeError):
    pass


def protected_json(path: Path) -> dict:
    """Refuse symlinks and writable/non-root ancestors, including the file."""
    path = Path(path)
    if not path.is_absolute():
        raise SafetyError("safety files require absolute paths")
    for p in [*reversed(path.parents), path]:
        s = p.lstat()
        if s.st_uid != 0 or s.st_mode & 0o022 or stat.S_ISLNK(s.st_mode):
            raise SafetyError("safety files and ancestors must be root-owned and immutable to the farm")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
            raise SafetyError("invalid safety file")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as f:
            value = json.load(f)
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise SafetyError("safety manifest must be an object")
    return value


def endpoint(url: str, *, https: bool = False) -> tuple[str, int]:
    u = urlsplit(url)
    if u.scheme not in (("https",) if https else ("http", "https")) or not u.hostname:
        raise SafetyError("invalid fixed endpoint")
    if u.username or u.password or u.query or u.fragment:
        raise SafetyError("endpoint cannot contain credentials, query, or fragment")
    try:
        ip = ipaddress.IPv4Address(u.hostname)
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError as exc:
        raise SafetyError("endpoints require literal IPv4 addresses; DNS is not allowed") from exc
    if ip.is_unspecified or ip.is_multicast or not 1 <= port <= 65535:
        raise SafetyError("invalid endpoint address")
    return str(ip), port



def validate_broker_manifest(m):
    if set(m) != {"version", "operator", "approval_reference", "model_url", "adapters", "broker"} or m["version"] != 2:
        raise SafetyError("invalid broker manifest")
    if any(not isinstance(m[k], str) or not m[k].strip() for k in ("operator", "approval_reference")):
        raise SafetyError("explicit operator approval required")
    b = m["broker"]
    if not isinstance(b, dict) or set(b) != {"host", "port", "ca_file", "client_cert", "client_key", "audit_public_key"}:
        raise SafetyError("invalid broker transport")
    host, port = endpoint("https://" + str(b["host"]) + ":" + str(b["port"]))
    if type(b["port"]) is not int or b["port"] != port:
        raise SafetyError("explicit broker port required")
    for key in ("ca_file", "client_cert", "client_key"):
        if not isinstance(b[key], str) or not Path(b[key]).is_absolute():
            raise SafetyError("explicit broker session files required")
    if not isinstance(b["audit_public_key"], str) or len(b["audit_public_key"]) != 64:
        raise SafetyError("explicit broker audit public key required")
    try:
        bytes.fromhex(b["audit_public_key"])
    except ValueError as exc:
        raise SafetyError("invalid audit key") from exc
    if m["model_url"] is not None and m["model_url"] != f"https://{host}:{port}/v1":
        raise SafetyError("inference must use the same restricted broker endpoint")
    if not isinstance(m["adapters"], list):
        raise SafetyError("explicit adapter list required")
    names = set()
    for a in m["adapters"]:
        common = {"name", "kind", "service"}
        if not isinstance(a, dict) or a.get("kind") not in {"fixed_json", "fixed_payment"}:
            raise SafetyError("unknown adapter kind")
        expected = common | ({"payee", "hard_cap_cents", "approval_threshold_cents"} if a["kind"] == "fixed_payment" else set())
        if set(a) != expected or not isinstance(a["name"], str) or not a["name"].startswith("real.") or a["name"] in names:
            raise SafetyError("invalid broker adapter")
        if not isinstance(a["service"], str) or not a["service"] or len(a["service"]) > 128:
            raise SafetyError("fixed broker service required")
        names.add(a["name"])
        if a["kind"] == "fixed_payment":
            if not isinstance(a["payee"], str) or not a["payee"] or any(type(a[k]) is not int or a[k] < 0 for k in ("hard_cap_cents", "approval_threshold_cents")):
                raise SafetyError("fixed payee/caps required")
            if not 1 <= a["hard_cap_cents"] or a["approval_threshold_cents"] > a["hard_cap_cents"]:
                raise SafetyError("invalid payment limits")
    return {(host, port)}

def validate_manifest(m: dict) -> set[tuple[str, int]]:
    if m.get("version") == 2:
        return validate_broker_manifest(m)
    if set(m) != {"version", "operator", "approval_reference", "model_url", "adapters"}:
        raise SafetyError("unexpected or missing safety manifest fields")
    if m["version"] != 1 or not isinstance(m["operator"], str) or not m["operator"].strip():
        raise SafetyError("explicit operator identity required")
    if not isinstance(m["approval_reference"], str) or not m["approval_reference"].strip():
        raise SafetyError("explicit approval reference required")
    destinations = {endpoint(m["model_url"])} if m["model_url"] is not None else set()
    if not isinstance(m["adapters"], list):
        raise SafetyError("adapters must be a list")
    names = set()
    for a in m["adapters"]:
        common = {"name", "kind", "endpoint", "credential_file"}
        if not isinstance(a, dict) or a.get("kind") not in {"fixed_json", "fixed_payment"}:
            raise SafetyError("unknown adapter kind")
        expected = common | ({"payee", "hard_cap_cents", "approval_threshold_cents"} if a["kind"] == "fixed_payment" else set())
        if set(a) != expected:
            raise SafetyError("unexpected or missing adapter configuration")
        if not isinstance(a["name"], str) or not a["name"].startswith("real.") or a["name"] in names:
            raise SafetyError("unique real.* adapter name required")
        names.add(a["name"])
        destinations.add(endpoint(a["endpoint"], https=True))
        if not isinstance(a["credential_file"], str) or not Path(a["credential_file"]).is_absolute():
            raise SafetyError("explicit credential file required")
        if a["kind"] == "fixed_payment":
            if not isinstance(a["payee"], str) or not a["payee"].strip():
                raise SafetyError("fixed payee required")
            for k in ("hard_cap_cents", "approval_threshold_cents"):
                if type(a[k]) is not int or a[k] < 0:
                    raise SafetyError("payment limits must be non-negative integer cents")
            if a["hard_cap_cents"] < 1 or a["approval_threshold_cents"] > a["hard_cap_cents"]:
                raise SafetyError("invalid payment limits")
    return destinations


class NetworkBoundary:
    def __init__(self, config):
        try:
            if os.name != "posix" or os.geteuid() == 0:
                raise SafetyError("farm must run unprivileged inside the Linux safety launcher")
            status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
            if any(int(status[k].strip(), 16) for k in ("CapEff", "CapPrm", "CapInh", "CapAmb", "CapBnd")):
                raise SafetyError("farm must have no Linux capabilities")
            if status["NoNewPrivs"].strip() != "1":
                raise SafetyError("no_new_privs is required")
            self.evidence = protected_json(Path("/run/qwenomatic-boundaries") / (str(os.stat("/proc/self/ns/net").st_ino) + ".json"))
            if self.evidence["namespace"] != os.readlink("/proc/self/ns/net") or self.evidence["uid"] != os.geteuid():
                raise SafetyError("namespace evidence mismatch")
            self.manifest = protected_json(Path(self.evidence["manifest_path"]))
            destinations = validate_manifest(self.manifest)
            if self.manifest["version"] == 1 and (self.manifest["adapters"] or self.manifest["model_url"] is not None):
                raise SafetyError("legacy direct-access manifests require broker migration")
            if digest(self.manifest) != self.evidence["manifest_digest"]:
                raise SafetyError("operator configuration changed; relaunch required")
            if sorted([list(d) for d in destinations]) != self.evidence["destinations"]:
                raise SafetyError("firewall allowlist mismatch")
            if self.manifest["version"] == 2:
                import hashlib
                for key in ("ca_file", "client_cert", "client_key"):
                    path = Path(self.manifest["broker"][key])
                    for item in (path, *path.parents):
                        st = item.lstat()
                        if item.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022:
                            raise SafetyError("unsafe broker session file")
                    if hashlib.sha256(path.read_bytes()).hexdigest() != self.evidence["credential_fingerprints"][key]:
                        raise SafetyError("broker session changed; relaunch required")
            inference = config.farm["inference"]
            if inference.get("backend", "simulated") == "simulated":
                if self.manifest["model_url"] is not None:
                    raise SafetyError("simulation must have no model destination")
            elif inference.get("backend") == "openai_compatible":
                c = inference["openai_compatible"]
                if c["base_url"].rstrip("/") != self.manifest["model_url"].rstrip("/") or not c.get("local", True):
                    raise SafetyError("model endpoint must match the operator-approved local server")
                if c.get("api_key") or inference.get("escalation", {}).get("enabled"):
                    raise SafetyError("inline inference credentials and cloud escalation are prohibited")
            else:
                raise SafetyError("unsupported inference backend")
            self.check()
        except SafetyError:
            raise
        except Exception as exc:
            raise SafetyError("network boundary could not be confirmed") from exc

    def check(self):
        """Only a kernel policy rejection counts. Timeout/refusal is not proof."""
        try:
            if os.readlink("/proc/self/ns/net") != self.evidence["namespace"]:
                raise SafetyError("network namespace changed")
            host, port = self.evidence["canary"]
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                # Linux reports ICMP administratively prohibited as EHOSTUNREACH.
                # Verify the actual ICMP code from the socket error queue rather
                # than accepting a generic no-route error with the same errno.
                s.setsockopt(socket.IPPROTO_IP, 11, 1)  # IP_RECVERR
                try:
                    s.connect((host, port))
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EPERM):
                        self.last_rejection = {"errno": exc.errno}
                    elif exc.errno == errno.EHOSTUNREACH:
                        _, ancillary, _, _ = s.recvmsg(1, 256, socket.MSG_ERRQUEUE)
                        proof = None
                        for level, kind, data in ancillary:
                            if level == socket.IPPROTO_IP and kind == 11 and len(data) >= 16:
                                number, origin, typ, code, _, _, _ = struct.unpack("=IBBBBII", data[:16])
                                if number == errno.EHOSTUNREACH and origin == 2 and typ == 3 and code == 13:
                                    proof = {"errno": number, "icmp_type": typ, "icmp_code": code}
                        if proof is None:
                            raise SafetyError("canary failure has no administrative rejection proof") from exc
                        self.last_rejection = proof
                    else:
                        raise SafetyError("canary failed without a kernel policy rejection") from exc
                else:
                    raise SafetyError("unapproved outbound connection succeeded")
        except SafetyError:
            raise
        except Exception as exc:
            raise SafetyError("network boundary check failed") from exc

    def record(self, store):
        store.append(EventType.ACCESS_APPROVED,
                     {"manifest_digest": digest(self.manifest), "approval_reference": self.manifest["approval_reference"],
                      "destinations": self.evidence["destinations"],
                      "adapters": [{k: v for k, v in a.items() if k != "credential_file"} for a in self.manifest["adapters"]],
                      "credential_fingerprints": self.evidence["credential_fingerprints"]},
                     author="operator:" + self.manifest["operator"],
                     idempotency_key="access:" + digest({"manifest": self.manifest,
                                                         "credentials": self.evidence["credential_fingerprints"]}))
        store.append(EventType.NETWORK_BARRIER_VERIFIED,
                     {"namespace": self.evidence["namespace"], "manifest_digest": digest(self.manifest),
                      "canary": self.evidence["canary"], "result": "kernel_rejected",
                      "kernel_error": self.last_rejection})
