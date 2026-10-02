"""Fixed-contract outbound adapters. Model-controlled input is only data.

The gateway queues durable intents. Dispatch happens only after their ledger
transaction commits. A durable attempted marker precedes the network call:
ambiguous results never trigger an automatic retry.
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import ssl
from urllib.parse import urlsplit

from runtime.tools.base import ToolAdapter, ToolError
from storage.events import EventType, digest
from supervisor.accounting.ledger import TrustedAdapter
from .boundary import protected_json, endpoint, SafetyError


class FixedAdapter(ToolAdapter, TrustedAdapter):
    real_world = True
    supports_spend_limit = False  # never silently change an approved payment

    def __init__(self, spec, boundary, ledger):
        self.spec = dict(spec)
        self.name = spec["name"]
        self.boundary = boundary
        self.ledger = ledger
        self.store = ledger.store
        self.payment = spec["kind"] == "fixed_payment"
        self.action_class = "payments.fixed" if self.payment else "external.fixed_write"
        self.args_schema = {"amount_cents": "int"} if self.payment else {"text": "str"}
        self.description = ("Request a capped payment through an operator-fixed channel."
                            if self.payment else "Submit plain text to an operator-fixed channel.")
        secret = protected_json(Path(spec["credential_file"]))
        if digest(secret) != boundary.evidence["credential_fingerprints"][self.name]:
            raise SafetyError("credential changed; operator approval and relaunch required")
        if set(secret) != {"token"} or not isinstance(secret["token"], str) or not secret["token"]:
            raise SafetyError("invalid credential file")
        self._token = secret["token"]
        if "\r" in self._token or "\n" in self._token:
            raise SafetyError("invalid credential token")

    def validate(self, args):
        errors = super().validate(args)
        if errors:
            return errors
        if self.payment:
            if not 1 <= args["amount_cents"] <= self.spec["hard_cap_cents"]:
                return ["payment exceeds fixed hard cap or is non-positive"]
        elif len(args["text"].encode("utf-8")) > 4096:
            return ["text exceeds 4096 bytes"]
        return []

    def classify(self, args):
        if self.payment and type(args.get("amount_cents")) is int:
            if args["amount_cents"] >= self.spec["approval_threshold_cents"]:
                return "payments.material"
        return self.action_class

    def spend(self, args):
        return args["amount_cents"] / 100 if self.payment else 0.0

    def invoke(self, args, ctx):
        # Only called by the existing gateway, after token/policy/budget checks.
        self.boundary.check()
        payload = {"invocation_id": ctx.invocation_id, "tool": self.name, "args": args,
                   "step_id": ctx.step_id, "tick": ctx.tick, "occurred_at": ctx.now.isoformat(),
                   "manifest_digest": digest(self.boundary.manifest)}
        with self.store.transaction():
            if self.payment:
                self.ledger.record(
                    self, agent_id=ctx.agent_id, lineage_id=ctx.lineage_id, generation_id=ctx.generation_id,
                    type="expense", category="external_spend", amount=self.spend(args),
                    external_reference="outbound:" + ctx.invocation_id,
                    occurred_at=ctx.now.isoformat(), observed_at=ctx.now.isoformat(),
                    step_id=ctx.step_id, step_generation=ctx.generation_id, tick=ctx.tick,
                    counterparty=self.spec["payee"], extra={"kind": "outbound_reservation",
                                                          "reconciliation": "required_until_confirmed"})
            self.store.append(EventType.OUTBOUND_QUEUED, payload, author=self.author,
                              agent_id=ctx.agent_id, lineage_id=ctx.lineage_id, generation_id=ctx.generation_id,
                              idempotency_key="outbound:" + ctx.invocation_id)
        # No credentials, destination, payee, or raw provider response reaches the model.
        return {"queued": True, "receipt": ctx.invocation_id}

    def _send(self, args, invocation_id):
        """Trusted dispatcher only; fixed HTTPS endpoint, no proxies/redirects/DNS."""
        host, port = endpoint(self.spec["endpoint"], https=True)
        path = urlsplit(self.spec["endpoint"]).path or "/"
        body = ({"amount_cents": args["amount_cents"], "payee": self.spec["payee"], "currency": "USD"}
                if self.payment else {"text": args["text"]})
        connection = http.client.HTTPSConnection(host, port, timeout=10, context=ssl.create_default_context())
        try:
            connection.request("POST", path, body=json.dumps(body).encode("utf-8"),
                               headers={"Content-Type": "application/json",
                                        "Authorization": "Bearer " + self._token,
                                        "Idempotency-Key": invocation_id})
            response = connection.getresponse()
            # Never follow redirects or expose provider content/headers.
            return 200 <= response.status < 300
        finally:
            connection.close()


def register_adapters(boundary, registry, ledger):
    for spec in boundary.manifest["adapters"]:
        tool = FixedAdapter(spec, boundary, ledger)
        registry.register(tool)
        ledger.register_adapter(tool)
