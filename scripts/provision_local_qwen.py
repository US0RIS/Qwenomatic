#!/usr/bin/env python3
"""Provision a single-WSL, inference-only Qwenomatic broker.

This creates the root-owned mTLS session and broker configuration needed for a
farm to reach Ollama through the existing kernel-enforced broker boundary. It
does not install Ollama or change the simulation profile.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID


def write(path: Path, data: bytes, mode: int = 0o444, group: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    if group:
        import grp
        os.chown(path, 0, grp.getgrnam(group).gr_gid)


def json_write(path: Path, value: dict, mode: int = 0o444, group: str | None = None):
    write(path, (json.dumps(value, indent=2) + "\n").encode(), mode, group)


def update_timeouts(root: Path):
    """Upgrade an installed broker without rotating certificates or touching state."""
    from supervisor.safety.boundary import protected_json
    if Path('/run/qwenomatic-service-broker.json').exists():
        raise SystemExit('stop the broker before changing its protected configuration')
    broker_path = root / 'private/broker.json'
    broker = protected_json(broker_path)
    if not isinstance(broker.get('inference'), dict):
        raise SystemExit('installed broker has no fixed inference endpoint')
    farm_path = root / 'config/farm.yaml'
    for item in (farm_path, *farm_path.parents):
        info = item.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise SystemExit(f'unsafe installed configuration path: {item}')
    farm = yaml.safe_load(farm_path.read_text())
    if farm['inference']['backend'] != 'openai_compatible':
        raise SystemExit('installed farm is not configured for real-model inference')
    broker['inference']['timeout_seconds'] = 120
    farm['inference']['openai_compatible']['timeout_seconds'] = 140
    json_write(broker_path, broker)
    write(farm_path, yaml.safe_dump(farm, sort_keys=False).encode())
    print('Updated broker/farm timeouts; certificates and ledger were preserved. Restart the broker.')


def repair_tls(root: Path, farm_user: str, broker_user: str):
    """Replace the incompatible TLS chain while retaining audit identity and state."""
    from supervisor.safety.boundary import protected_json
    if Path('/run/qwenomatic-service-broker.json').exists():
        raise SystemExit('stop the broker before replacing its TLS certificates')
    private = root / 'private'
    targets = [private / name for name in ('ca.pem', 'broker.pem', 'broker.key',
                                           'farm.pem', 'farm.key', 'policy.json', 'broker.json')]
    for path in (root / 'manifest.json', *targets):
        for item in (path, *path.parents):
            info = item.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                raise SystemExit(f'unsafe installed TLS path: {item}')
    broker_path, policy_path = private / 'broker.json', private / 'policy.json'
    broker = protected_json(broker_path)
    policy = protected_json(policy_path)
    manifest = protected_json(root / 'manifest.json')
    if broker['policy'] != policy or set(policy['farms']) != {'farm'}:
        raise SystemExit('installed broker policy differs from the local-Qwen template')
    if any(broker[k] != str(private / name) for k, name in (
            ('client_ca', 'ca.pem'), ('tls_cert', 'broker.pem'), ('tls_key', 'broker.key'))):
        raise SystemExit('installed broker TLS paths differ from the local-Qwen template')
    if any(manifest['broker'][k] != str(private / name) for k, name in (
            ('ca_file', 'ca.pem'), ('client_cert', 'farm.pem'), ('client_key', 'farm.key'))):
        raise SystemExit('installed farm TLS paths differ from the local-Qwen template')
    old_cert = x509.load_pem_x509_certificate((private / 'farm.pem').read_bytes())
    old_fp = hashlib.sha256(old_cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    if policy['farms']['farm']['certificates'] != [old_fp]:
        raise SystemExit('installed client certificate does not match the broker policy')
    ca_pem, ca_key, ca_cert = make_ca()
    address = str(ipaddress.IPv4Address(broker['listen'][0]))
    broker_pem, broker_key, _ = cert(ca_key, ca_cert, address, address)
    farm_pem, farm_key, new_fp = cert(ca_key, ca_cert, 'qwenomatic-farm')
    policy['farms']['farm']['certificates'] = [new_fp]
    broker['policy'] = policy
    # The broker must be stopped. Each run writes a complete new chain, so a
    # failed partial replacement can be retried without changing audit keys.
    write(private / 'ca.pem', ca_pem)
    write(private / 'broker.pem', broker_pem)
    write(private / 'broker.key', broker_key, 0o440, broker_user)
    write(private / 'farm.pem', farm_pem)
    write(private / 'farm.key', farm_key, 0o440, farm_user)
    json_write(policy_path, policy)
    json_write(broker_path, broker)
    print('Replaced TLS certificates and updated client fingerprint; audit key, manifest and ledger were preserved. Restart the broker.')


def make_ca():
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Qwenomatic local CA')])
    now = datetime.now(timezone.utc)
    ca_cert = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
               .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
               .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                            key_encipherment=False, data_encipherment=False,
                                            key_agreement=False, key_cert_sign=True, crl_sign=True,
                                            encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
               .sign(ca_key, hashes.SHA256()))
    return ca_cert.public_bytes(serialization.Encoding.PEM), ca_key, ca_cert


def cert(ca_key, ca_cert, name: str, ip: str | None = None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_cert.subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365)))
    names = [x509.DNSName(name)]
    if ip:
        names.append(x509.IPAddress(ipaddress.ip_address(ip)))
    builder = (builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                            key_encipherment=True, data_encipherment=False,
                                            key_agreement=False, key_cert_sign=False, crl_sign=False,
                                            encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False))
    value = builder.sign(ca_key, hashes.SHA256())
    pem = value.public_bytes(serialization.Encoding.PEM)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return pem, private, hashlib.sha256(value.public_bytes(serialization.Encoding.DER)).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default="/etc/qwenomatic/local-qwen")
    p.add_argument("--repo", default="/opt/qwenomatic")
    p.add_argument("--runtime", default="/opt/qwenomatic-runtime/bin/python")
    p.add_argument("--ollama-host", help="Windows host address visible from WSL")
    p.add_argument("--ollama-model", default="qwen3:14b")
    p.add_argument("--update-timeouts", action="store_true", help="upgrade an existing installation without rotating keys")
    p.add_argument("--repair-tls", action="store_true", help="repair Python 3.14 TLS certificates without replacing audit keys or ledger")
    p.add_argument("--farm-user", default="qwenomatic")
    p.add_argument("--broker-user", default="qbroker")
    a = p.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run this provisioning command with sudo")
    root, repo = Path(a.root), Path(a.repo)
    if a.update_timeouts and a.repair_tls:
        p.error('choose one repair operation at a time')
    if a.update_timeouts:
        update_timeouts(root)
        return
    if a.repair_tls:
        repair_tls(root, a.farm_user, a.broker_user)
        return
    if not a.ollama_host:
        p.error('--ollama-host is required for initial provisioning')
    ipaddress.IPv4Address(a.ollama_host)
    if not (repo / "config/farm.yaml").is_file():
        raise SystemExit(f"repository not found: {repo}")
    import pwd
    try:
        pwd.getpwnam(a.farm_user)
    except KeyError:
        raise SystemExit(f"missing service user: {a.farm_user}")
    try:
        pwd.getpwnam(a.broker_user)
    except KeyError:
        subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", a.broker_user], check=True)
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chown(root, 0, 0)
    (root / "private").mkdir(mode=0o755, exist_ok=True); (root / "private").chmod(0o755); os.chown(root / "private", 0, 0)
    (root / "state").mkdir(mode=0o700, exist_ok=True)
    os.chown(root / "state", 0, 0)
    private, state = root / "private", root / "state"
    broker_uid = pwd.getpwnam(a.broker_user).pw_uid
    broker_gid = pwd.getpwnam(a.broker_user).pw_gid
    os.chown(state, broker_uid, broker_gid)

    ca_pem, ca_key, ca_cert = make_ca()
    write(private / "ca.pem", ca_pem, 0o444)
    broker_pem, broker_key, _ = cert(ca_key, ca_cert, "10.204.1.2", "10.204.1.2")
    client_pem, client_key, client_fp = cert(ca_key, ca_cert, "qwenomatic-farm")
    write(private / "broker.pem", broker_pem, 0o444)
    write(private / "broker.key", broker_key, 0o440, a.broker_user)
    write(private / "farm.pem", client_pem, 0o444)
    write(private / "farm.key", client_key, 0o440, a.farm_user)
    audit = ed25519.Ed25519PrivateKey.generate()
    write(private / "audit.key", audit.private_bytes_raw(), 0o440, a.broker_user)
    audit_public = audit.public_key().public_bytes_raw().hex()

    limits = {"requests": 10000, "messages": 10000, "purchases": 0, "bytes": 100000000,
              "spend_cents": 0, "concurrency": 4, "period_seconds": 3600}
    policy = {"version": "local-qwen-v1", "operator": "owner", "approval_reference": "local-qwen-setup",
              "farms": {"farm": {"limits": limits, "certificates": [client_fp]}}, "services": {}}
    json_write(private / "policy.json", policy)
    broker_cfg = {"policy": policy, "signing_key": str(private / "audit.key"),
                  "tls_cert": str(private / "broker.pem"), "tls_key": str(private / "broker.key"),
                  "client_ca": str(private / "ca.pem"), "listen": ["10.204.1.2", 9443],
                  "state_dir": str(state), "slots": 4,
                  "inference": {"endpoint": f"http://{a.ollama_host}:11434/v1/chat/completions",
                                 "model": a.ollama_model, "max_tokens": 4096, "max_messages": 32,
                                 "max_content_bytes": 65536, "requests_per_period": 10000,
                                 "period_seconds": 3600, "concurrency": 4,
                                 "timeout_seconds": 120}}
    json_write(private / "broker.json", broker_cfg)
    json_write(root / "deploy-broker.json", {"service": str(private / "broker.json"),
                                               "subnet": "10.204.1.0/30", "peers": ["10.204.1.1"]})
    manifest = {"version": 2, "operator": "owner", "approval_reference": "local-qwen-setup",
                "model_url": "https://10.204.1.2:9443/v1", "adapters": [],
                "broker": {"host": "10.204.1.2", "port": 9443, "ca_file": str(private / "ca.pem"),
                           "client_cert": str(private / "farm.pem"), "client_key": str(private / "farm.key"),
                           "audit_public_key": audit_public}}
    json_write(root / "manifest.json", manifest)

    cfg = root / "config"
    if cfg.exists(): shutil.rmtree(cfg)
    shutil.copytree(repo / "config", cfg)
    farm_path = cfg / "farm.yaml"
    farm = yaml.safe_load(farm_path.read_text())
    farm["inference"]["backend"] = "openai_compatible"
    farm["inference"]["openai_compatible"].update(base_url="https://10.204.1.2:9443/v1",
                                                     model=a.ollama_model, local=True,
                                                     timeout_seconds=140)
    write(farm_path, yaml.safe_dump(farm, sort_keys=False).encode(), 0o444)
    for path in cfg.rglob("*"):
        if path.is_file(): path.chmod(0o444); os.chown(path, 0, 0)
    data = Path("/var/lib/qwenomatic/local-qwen")
    data.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(data, pwd.getpwnam(a.farm_user).pw_uid, pwd.getpwnam(a.farm_user).pw_gid)
    print(json.dumps({"manifest": str(root / "manifest.json"), "config_dir": str(cfg),
                      "broker_deploy": str(root / "deploy-broker.json"), "data_dir": str(data),
                      "ollama_endpoint": f"http://{a.ollama_host}:11434/v1", "model": a.ollama_model}, indent=2))


if __name__ == "__main__":
    main()
