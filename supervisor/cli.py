"""Operator command line.

    qwenomatic init                       create Generation Zero
    qwenomatic run [--generations N]      run the farm (auto-advances generations)
    qwenomatic status                     summary from the ledger (safe while running)
    qwenomatic dashboard [--port 8765]    read-only web dashboard
    qwenomatic verify                     hash chain + accounting and selection replay
    qwenomatic lineage AGENT_ID           ancestry of one agent
    qwenomatic approvals                  pending human approvals
    qwenomatic approve|deny APPROVAL_ID   resolve an approval
    qwenomatic stop --reason TEXT         emergency stop: halt work, revoke capabilities
    qwenomatic resume                     resume after a stop

Commands that write go through the running supervisor's inbox when one holds
the lock, so the supervisor stays the ledger's single writer.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from storage.events import EventStore, FarmState

from .config import FarmConfig


def _config(args: argparse.Namespace) -> FarmConfig:
    overrides: dict[str, Any] = {"farm": {}}
    if getattr(args, "backend", None):
        overrides["farm"]["inference"] = {"backend": args.backend}
    if getattr(args, "base_url", None):
        overrides["farm"].setdefault("inference", {})["openai_compatible"] = {"base_url": args.base_url}
    if getattr(args, "model", None):
        overrides["farm"].setdefault("inference", {}).setdefault("openai_compatible", {})["model"] = args.model
    if getattr(args, "max_concurrency", None) is not None:
        overrides["farm"].setdefault("inference", {}).setdefault("openai_compatible", {})["max_concurrency"] = int(args.max_concurrency)
    if getattr(args, "wall_clock", False):
        overrides["farm"]["clock"] = {"mode": "wall"}
    if getattr(args, "thinking_mode", None):
        overrides["farm"]["runtime"] = {"thinking": {"mode": args.thinking_mode}}
    return FarmConfig.load(args.config_dir, data_dir=args.data_dir, overrides=overrides)


def _db(cfg: FarmConfig) -> Path:
    return cfg.data_dir / "ledger.sqlite3"


def _open_supervisor(cfg: FarmConfig):
    from .core import Supervisor, SupervisorLocked

    try:
        return Supervisor(cfg)
    except SupervisorLocked:
        return None


def _operator_command(cfg: FarmConfig, command: dict[str, Any]) -> int:
    from .core import submit_command

    sup = _open_supervisor(cfg)
    if sup is None:
        path = submit_command(cfg.data_dir, command)
        print(f"supervisor is running; queued {command['command']} as {path.name}")
        return 0
    try:
        if command["command"] == "stop":
            sup.emergency_stop(command["reason"], operator=command["operator"])
        elif command["command"] == "resume":
            sup.resume(operator=command["operator"])
        else:
            sup.resolve_approval(command["approval_id"], granted=command["command"] == "approve",
                                 operator=command["operator"], note=command.get("note", ""))
        print(f"{command['command']}: done")
    finally:
        sup.close()
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    cfg = _config(args)
    from .core import Supervisor

    sup = Supervisor(cfg)
    try:
        sup.bootstrap()
        print(f"generation {sup.state.current_generation} with {len(sup.state.active_agents())} agents in {cfg.data_dir}")
    finally:
        sup.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config(args)
    from .core import Supervisor

    sup = Supervisor(cfg)
    try:
        sup.bootstrap()
        start = sup.state.current_generation
        last_gen = start
        for_gens = args.generations
        n = 0
        while True:
            if for_gens is not None and sup.state.current_generation >= start + for_gens:
                break
            if args.ticks is not None and n >= args.ticks:
                break
            r = sup.tick()
            n += 1
            if r.halted:
                print(f"farm halted: {sup.state.halt_reason}")
                break
            if sup.state.current_generation != last_gen:
                last_gen = sup.state.current_generation
                _print_generation_summary(sup, last_gen - 1)
            sup.clock.sleep_until_next_tick()
    except KeyboardInterrupt:
        print("interrupted; state is persisted and will recover on restart")
    finally:
        sup.close()
    return 0


def _print_generation_summary(sup, g: int) -> None:
    fit = sup.state.fitness.get(g, {})
    plan = sup.state.selections.get(g, {}).get("plan", {})
    net = sum(c.net_realized for c in sup.state.counters.get(g, {}).values())
    eligible = [r for r in fit.values() if r["eligible"]]
    print(f"generation {g} closed: net realized {net:,.2f} | eligible {len(eligible)}/{len(fit)} | "
          f"retired {len(plan.get('retirements', []))} | offspring {len(plan.get('offspring', []))}")


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _config(args)
    if not _db(cfg).exists():
        print("no ledger yet; run `qwenomatic init`")
        return 1
    from dashboard.metrics import LedgerView

    view = LedgerView(EventStore(_db(cfg), read_only=True))
    view.refresh()
    o = view.overview()
    for k in ("generation", "time_remaining_hours", "population", "status_counts", "halted", "gross_revenue",
              "net_realized_profit", "unrealized", "external_spend", "inference_tokens", "gpu_seconds",
              "human_interventions", "policy_violations", "events"):
        print(f"{k:>22}: {o[k]}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    cfg = _config(args)
    if not _db(cfg).exists():
        print("no ledger yet; run `qwenomatic init`")
        return 1
    from dashboard.server import serve

    server = serve(_db(cfg), args.host, args.port)
    print(f"dashboard on http://{args.host}:{args.port} (read-only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = _config(args)
    from .audit import verify

    sup = _open_supervisor(cfg)
    if sup is None:
        print("stop the running supervisor first (verify replays selection with a full supervisor)")
        return 1
    try:
        report = verify(sup)
    finally:
        sup.close()
    print(json.dumps(report, indent=2, default=str))
    ok = (report["hash_chain"]["ok"] and report["accounting"]["ok"] and all(report["selection_replay"].values())
          and report["generations_started_once"])
    return 0 if ok else 2


def cmd_lineage(args: argparse.Namespace) -> int:
    cfg = _config(args)
    from .evolution.lineage import ancestry

    state = FarmState().replay(EventStore(_db(cfg), read_only=True).iter_events())
    matches = [a for a in state.agents if a.startswith(args.agent_id)]
    if len(matches) != 1:
        print(f"{len(matches)} agents match {args.agent_id!r}")
        return 1
    for depth, agent_id in enumerate(ancestry(state, matches[0])):
        a = state.agents[agent_id]
        diffs = ", ".join(f"{m['type']}: {m['before']!r}→{m['after']!r}" for m in a.mutations) or a.origin
        print(f"{'  ' * depth}{agent_id} gen {a.generation_born} [{a.status}] {diffs}")
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    cfg = _config(args)
    from .policy.approvals import pending

    state = FarmState().replay(EventStore(_db(cfg), read_only=True).iter_events())
    for a in pending(state):
        print(f"{a['approval_id']}  agent {a['agent_id'][:8]}  {a['request']['tool']}  {a['reason']}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    return _operator_command(_config(args), {"command": args.command, "approval_id": args.approval_id,
                                             "operator": args.operator, "note": args.note or ""})


def cmd_stop(args: argparse.Namespace) -> int:
    return _operator_command(_config(args), {"command": "stop", "reason": args.reason, "operator": args.operator})


def cmd_resume(args: argparse.Namespace) -> int:
    return _operator_command(_config(args), {"command": "resume", "operator": args.operator})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qwenomatic", description="Qwenomatic evolutionary agent farm")
    p.add_argument("--config-dir", default=None, help="directory with farm.yaml, policy.yaml, fitness.yaml")
    p.add_argument("--data-dir", default=None, help="ledger/workspace directory (default: farm.data_dir)")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, fn, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, help="create Generation Zero")
    r = add("run", cmd_run, help="run the farm")
    r.add_argument("--generations", type=int, default=None)
    r.add_argument("--ticks", type=int, default=None)
    r.add_argument("--backend", choices=["simulated", "openai_compatible"])
    r.add_argument("--base-url")
    r.add_argument("--model")
    r.add_argument("--max-concurrency", type=int, help="client inference concurrency; match the model server")
    r.add_argument("--wall-clock", action="store_true", help="real time instead of simulated ticks")
    r.add_argument("--thinking-mode", choices=["server", "on", "off", "adaptive"],
                   help="override runtime.thinking.mode (EXPERIMENTS.md E13 arms: adaptive vs on)")
    add("status", cmd_status, help="summary from the ledger")
    d = add("dashboard", cmd_dashboard, help="read-only web dashboard")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8765)
    add("verify", cmd_verify, help="replay and verify the ledger")
    lg = add("lineage", cmd_lineage, help="show an agent's ancestry")
    lg.add_argument("agent_id")
    add("approvals", cmd_approvals, help="list pending approvals")
    for name in ("approve", "deny"):
        a = add(name, cmd_resolve, help=f"{name} a pending request")
        a.add_argument("approval_id")
        a.add_argument("--note")
        a.add_argument("--operator", default=getpass.getuser())
    s = add("stop", cmd_stop, help="emergency stop")
    s.add_argument("--reason", required=True)
    s.add_argument("--operator", default=getpass.getuser())
    rs = add("resume", cmd_resume, help="resume after a stop")
    rs.add_argument("--operator", default=getpass.getuser())
    return p


def main(argv: list[str] | None = None) -> int:
    from .config import ConfigNotFound

    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ConfigNotFound as exc:
        print(f"qwenomatic: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
