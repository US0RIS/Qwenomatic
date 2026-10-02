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
        # No provider token/file is read or held in this process. Tests may use
        # historical contracts, but production registration requires manifest v2.
        self.broker_receipt = None

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
        from broker.client import Client
        if self.boundary.manifest.get("version") != 2:
            raise SafetyError("direct provider transport is prohibited")
        accepted, receipt = Client(self.boundary.manifest["broker"]).submit(self.spec["service"], args, invocation_id)
        self.broker_receipt = receipt
        return accepted


def register_adapters(boundary, registry, ledger):
    if boundary.manifest["adapters"] and boundary.manifest.get("version") != 2:
        raise SafetyError("legacy adapters require independent broker")
    for spec in boundary.manifest["adapters"]:
        tool = FixedAdapter(spec, boundary, ledger)
        registry.register(tool)
        ledger.register_adapter(tool)
