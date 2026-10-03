"""Broker-owned transactions, permissions, aggregate ceilings and signed audit."""
import base64
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from .network import service_url
from .protocol import Rejected, canonical, decode, digest, fields, identifier

DIMENSIONS = ("requests", "messages", "purchases", "bytes", "spend_cents")


def limits(value):
    fields(value, (*DIMENSIONS, "concurrency", "period_seconds"))
    if any(type(v) is not int or v < 0 for v in value.values()) or value["concurrency"] < 1 or value["period_seconds"] < 1:
        raise Rejected("invalid explicit ceilings")


def validate_policy(policy):
    fields(policy, ("version", "operator", "approval_reference", "farms", "services"))
    identifier(policy["version"])
    if not all(isinstance(policy[k], str) and policy[k].strip() for k in ("operator", "approval_reference")):
        raise Rejected("explicit operator approval required")
    if not isinstance(policy["farms"], dict) or not policy["farms"] or not isinstance(policy["services"], dict):
        raise Rejected("invalid principals/services")
    for farm, spec in policy["farms"].items():
        identifier(farm)
        fields(spec, ("limits", "certificates"))
        limits(spec["limits"])
        if not isinstance(spec["certificates"], list) or not spec["certificates"]:
            raise Rejected("explicit client certificate fingerprints required")
        for fp in spec["certificates"]:
            if not isinstance(fp, str) or len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
                raise Rejected("invalid certificate fingerprint")
    seen = set()
    for farm in policy["farms"].values():
        for fingerprint in farm["certificates"]:
            if fingerprint in seen:
                raise Rejected("ambiguous authenticated identity")
            seen.add(fingerprint)
    for name, spec in policy["services"].items():
        identifier(name)
        fields(spec, ("kind", "endpoint", "account", "credential_file", "payee", "hard_cap_cents", "limits", "farms"))
        if spec["kind"] not in ("fixed_json", "fixed_payment"):
            raise Rejected("unknown compiled adapter")
        service_url(spec["endpoint"])
        identifier(spec["account"])
        if not isinstance(spec["credential_file"], str) or not Path(spec["credential_file"]).is_absolute():
            raise Rejected("explicit broker credential path required")
        if type(spec["hard_cap_cents"]) is not int or spec["hard_cap_cents"] < 0:
            raise Rejected("invalid hard cap")
        if spec["kind"] == "fixed_payment" and (spec["hard_cap_cents"] < 1 or not isinstance(spec["payee"], str) or not spec["payee"]):
            raise Rejected("fixed payee/cap required")
        if spec["kind"] == "fixed_json" and (spec["payee"] is not None or spec["hard_cap_cents"] != 0):
            raise Rejected("text contract cannot authorize payment")
        limits(spec["limits"])
        if not isinstance(spec["farms"], list) or not spec["farms"] or any(f not in policy["farms"] for f in spec["farms"]):
            raise Rejected("unknown farm")
    # Shared accounts and destinations must have one consistent ceiling.
    for attr in ("account", "endpoint"):
        grouped = {}
        for spec in policy["services"].values():
            key = spec[attr] if attr == "account" else service_url(spec[attr])[0]
            if key in grouped and grouped[key] != spec["limits"]:
                raise Rejected("inconsistent shared-scope limits")
            grouped[key] = spec["limits"]
    return policy


def wire_request(policy, request):
    fields(request, ("invocation_id", "service", "args"))
    identifier(request["invocation_id"])
    identifier(request["service"])
    spec = policy["services"].get(request["service"])
    if spec is None:
        raise Rejected("unconfigured service")
    args = request["args"]
    if spec["kind"] == "fixed_payment":
        fields(args, ("amount_cents",))
        amount = args["amount_cents"]
        if type(amount) is not int or not 1 <= amount <= spec["hard_cap_cents"]:
            raise Rejected("invalid payment")
        body = {"amount_cents": amount, "payee": spec["payee"], "currency": "USD"}
        cost = {"requests": 1, "messages": 0, "purchases": 1, "bytes": len(canonical(body)), "spend_cents": amount}
    else:
        fields(args, ("text",))
        if not isinstance(args["text"], str) or len(args["text"].encode("utf-8")) > 4096:
            raise Rejected("invalid text")
        body = {"text": args["text"]}
        cost = {"requests": 1, "messages": 1, "purchases": 0, "bytes": len(canonical(body)), "spend_cents": 0}
    host, path = service_url(spec["endpoint"])
    wire = {"service": request["service"], "account": spec["account"], "method": "POST", "host": host,
            "path": path, "content_hash": digest(body), "idempotency_key": request["invocation_id"]}
    return spec, body, cost, digest(wire)


class Authority:
    def __init__(self, path, policy, signing_key, clock=time.time):
        self.policy = validate_policy(policy)
        self.policy_hash = digest(policy)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.key = Ed25519PrivateKey.from_private_bytes(signing_key)
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY, record BLOB NOT NULL, hash TEXT NOT NULL, signature TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS grants(farm TEXT, iid TEXT, request_hash TEXT NOT NULL, expires REAL NOT NULL, policy TEXT NOT NULL, operator TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(farm,iid));
          CREATE TABLE IF NOT EXISTS attempts(farm TEXT, iid TEXT, request_hash TEXT, request_digest TEXT, state TEXT, PRIMARY KEY(farm,iid));
          CREATE TABLE IF NOT EXISTS usage(scope TEXT, period INTEGER, dimension TEXT, value INTEGER NOT NULL, PRIMARY KEY(scope,period,dimension));
          CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT);
          CREATE TRIGGER IF NOT EXISTS no_audit_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append-only'); END;
          CREATE TRIGGER IF NOT EXISTS no_audit_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append-only'); END;
        ''')
        self.active = {}
        self.verify_audit()
        row = self.db.execute("SELECT value FROM settings WHERE name='policy'").fetchone()
        if row and row[0] != self.policy_hash:
            raise Rejected("policy changed; explicit operator migration required")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES('policy',?)", (self.policy_hash,))
        if self.db.execute("SELECT 1 FROM attempts WHERE state='attempted'").fetchone():
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('halted','1')")
        self._audit("broker_started", {"policy": self.policy_hash})

    @classmethod
    def migrate_local_tls_policy(cls, path, policy, signing_key, clock=time.time):
        """Operator maintenance for the single-farm TLS identity rotation.

        Retains usage, attempts and the signed audit chain. Old grants are
        revoked, and an unresolved attempted delivery still blocks migration.
        The caller must verify the fixed local-Qwen policy and run as qbroker.
        """
        validate_policy(policy)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        authority = cls.__new__(cls)
        authority.policy_hash = digest(policy)
        authority.key = Ed25519PrivateKey.from_private_bytes(signing_key)
        authority.clock = clock
        authority.lock = threading.RLock()
        authority.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        try:
            authority.db.execute("PRAGMA journal_mode=WAL")
            authority.db.execute("PRAGMA synchronous=FULL")
            authority.verify_audit()
            authority.db.execute("BEGIN IMMEDIATE")
            try:
                row = authority.db.execute("SELECT value FROM settings WHERE name='policy'").fetchone()
                if not row:
                    raise Rejected('existing broker policy required for TLS migration')
                if row[0] == authority.policy_hash:
                    authority.db.execute('COMMIT')
                    return False
                if authority.db.execute("SELECT 1 FROM attempts WHERE state='attempted'").fetchone():
                    raise Rejected('unresolved attempted delivery blocks policy migration')
                binding = None
                for (raw,) in authority.db.execute('SELECT record FROM audit ORDER BY seq DESC'):
                    record = decode(raw)
                    if record.get('kind') in ('broker_started', 'policy_migrated'):
                        binding = record
                        break
                bound_hash = (binding['payload'].get('policy') if binding['kind'] == 'broker_started'
                              else binding['payload'].get('new_policy')) if binding else None
                if bound_hash != row[0]:
                    raise Rejected('broker policy has no matching signed audit record')
                authority.db.execute('UPDATE grants SET revoked=1')
                authority.db.execute("UPDATE settings SET value=? WHERE name='policy'", (authority.policy_hash,))
                authority._audit('policy_migrated', {'old_policy': row[0], 'new_policy': authority.policy_hash,
                                                     'operator': 'owner', 'reason': 'local_qwen_tls_rotation'})
                authority.db.execute('COMMIT')
                return True
            except BaseException:
                authority.db.execute('ROLLBACK')
                raise
        finally:
            authority.db.close()

    def checked_time(self):
        # Period rollover and expiry must not be reset by a backwards wall clock.
        with self.lock:
            now = self.clock()
            if not isinstance(now, (float, int)) or not math.isfinite(now):
                raise Rejected("invalid broker clock")
            previous = self.db.execute("SELECT value FROM settings WHERE name='clock'").fetchone()
            if previous and now < float(previous[0]):
                raise Rejected("broker clock moved backwards")
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('clock',?)", (str(now),))
            return now

    def verify_audit(self):
        previous = "0" * 64
        expected_seq = 1
        for seq, raw, hash_value, signature in self.db.execute("SELECT seq,record,hash,signature FROM audit ORDER BY seq"):
            record = decode(raw)
            if seq != expected_seq or record["seq"] != seq or record["previous"] != previous or digest(record) != hash_value:
                raise Rejected("audit chain invalid")
            self.key.public_key().verify(base64.b64decode(signature, validate=True), raw)
            previous = hash_value
            expected_seq += 1

    def _audit(self, kind, payload):
        row = self.db.execute("SELECT seq,hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        record = {"seq": row[0] + 1 if row else 1, "previous": row[1] if row else "0" * 64,
                  "author": "broker", "time": self.checked_time(), "kind": kind, "payload": payload}
        raw = canonical(record)
        signature = base64.b64encode(self.key.sign(raw)).decode()
        self.db.execute("INSERT INTO audit VALUES(?,?,?,?)", (record["seq"], raw, digest(record), signature))
        return {"record": record, "signature": signature}

    def authenticate(self, fingerprint):
        for farm, spec in self.policy["farms"].items():
            if fingerprint in spec["certificates"]:
                return farm
        raise Rejected("unknown session identity")

    def grant(self, farm, request, expires, operator):
        identifier(operator)
        if farm not in self.policy["farms"] or farm not in self.policy["services"].get(request.get("service"), {}).get("farms", []):
            raise Rejected("principal not authorized")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires) or not self.checked_time() < expires <= self.checked_time() + 300:
            raise Rejected("grant expiry must be within five minutes")
        _, _, _, hash_value = wire_request(self.policy, request)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute("SELECT 1 FROM settings WHERE name='halted'").fetchone():
                    raise Rejected("halted")
                self.db.execute("INSERT INTO grants(farm,iid,request_hash,expires,policy,operator) VALUES(?,?,?,?,?,?)",
                                (farm, request["invocation_id"], hash_value, expires, self.policy_hash, operator))
                receipt = self._audit("operator_grant", {"farm": farm, "invocation_id": request["invocation_id"],
                                                       "request_hash": hash_value, "expires": expires, "operator": operator})
                self.db.execute("COMMIT")
                return receipt
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _scopes(self, farm, spec):
        return [("farm:" + farm, self.policy["farms"][farm]["limits"]),
                ("account:" + spec["account"], spec["limits"]),
                ("destination:" + service_url(spec["endpoint"])[0], spec["limits"])]

    def reserve(self, farm, request):
        spec, body, cost, hash_value = wire_request(self.policy, request)
        if farm not in spec["farms"]:
            raise Rejected("farm not permitted")
        scopes = self._scopes(farm, spec)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute("SELECT 1 FROM settings WHERE name='halted'").fetchone():
                    raise Rejected("halted")
                now = self.checked_time()
                grant = self.db.execute("SELECT request_hash,expires,policy,consumed,revoked FROM grants WHERE farm=? AND iid=?",
                                        (farm, request["invocation_id"])).fetchone()
                if not grant or grant[0] != hash_value or now >= grant[1] or grant[2] != self.policy_hash or grant[3] or grant[4]:
                    raise Rejected("permission unavailable")
                for scope, bounds in scopes:
                    if self.active.get(scope, 0) >= bounds["concurrency"]:
                        raise Rejected("concurrency limit")
                    period = int(now) // bounds["period_seconds"]
                    for dimension, amount in cost.items():
                        row = self.db.execute("SELECT value FROM usage WHERE scope=? AND period=? AND dimension=?", (scope, period, dimension)).fetchone()
                        if (row[0] if row else 0) + amount > bounds[dimension]:
                            raise Rejected("aggregate ceiling")
                self.db.execute("UPDATE grants SET consumed=1 WHERE farm=? AND iid=?", (farm, request["invocation_id"]))
                self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,'attempted')", (farm, request["invocation_id"], hash_value, digest(request)))
                for scope, bounds in scopes:
                    period = int(now) // bounds["period_seconds"]
                    for dimension, amount in cost.items():
                        self.db.execute("INSERT INTO usage VALUES(?,?,?,?) ON CONFLICT(scope,period,dimension) DO UPDATE SET value=value+excluded.value", (scope, period, dimension, amount))
                self._audit("attempted", {"farm": farm, "invocation_id": request["invocation_id"], "wire_hash": hash_value, "cost": cost})
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            for scope, _ in scopes:
                self.active[scope] = self.active.get(scope, 0) + 1
        return spec, body, scopes

    def finish(self, farm, iid, scopes, accepted):
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("UPDATE attempts SET state=? WHERE farm=? AND iid=?", ("accepted" if accepted else "unknown", farm, iid))
                attempt = self.db.execute("SELECT request_hash,request_digest FROM attempts WHERE farm=? AND iid=?", (farm, iid)).fetchone()
                if not attempt:
                    raise Rejected("unreserved result")
                receipt = self._audit("result", {"farm": farm, "invocation_id": iid, "request_hash": attempt[0], "request_digest": attempt[1], "status": "accepted" if accepted else "unknown_requires_reconciliation"})
                self.db.execute("COMMIT")
                return receipt
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            finally:
                for scope, _ in scopes:
                    self.active[scope] -= 1

    def revoke(self, farm, iid, operator):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("UPDATE grants SET revoked=1 WHERE farm=? AND iid=?", (farm, iid))
                receipt = self._audit("revoked", {"farm": farm, "invocation_id": iid, "operator": identifier(operator)})
                self.db.execute("COMMIT")
                return receipt
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def halt(self, operator):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES('halted','1')")
                self.db.execute("UPDATE grants SET revoked=1")
                receipt = self._audit("halted", {"operator": identifier(operator)})
                self.db.execute("COMMIT")
                return receipt
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
