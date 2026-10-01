"""Per-agent workspace tools with a quota and path confinement (DESIGN §12)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from storage.events.canonical import digest

from .base import ToolAdapter, ToolContext, ToolError

ESCAPE_CLASS = "security.unauthorized_access"


def _escapes(path: Any) -> bool:
    if not isinstance(path, str) or not path:
        return False
    p = PurePosixPath(path.replace("\\", "/"))
    return p.is_absolute() or ".." in p.parts or path.startswith("~")


def _confined(workspace: Path, path: str) -> Path:
    root = workspace.resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise ToolError("path outside workspace")
    return target


class WorkspaceWriteTool(ToolAdapter):
    name = "workspace.write"
    description = "Write a text artifact into your own workspace (relative path)."
    action_class = "workspace.write"
    args_schema = {"path": "str", "content": "str"}

    def __init__(self, quota_bytes: int) -> None:
        self.quota_bytes = quota_bytes

    def classify(self, args: dict[str, Any]) -> str:
        return ESCAPE_CLASS if isinstance(args, dict) and _escapes(args.get("path")) else self.action_class

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        ctx.workspace.mkdir(parents=True, exist_ok=True)
        target = _confined(ctx.workspace, args["path"])
        data = args["content"].encode("utf-8")
        used = sum(f.stat().st_size for f in ctx.workspace.rglob("*") if f.is_file() and f != target)
        if used + len(data) > self.quota_bytes:
            raise ToolError("workspace quota exceeded")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        rel = str(target.relative_to(ctx.workspace.resolve()))
        return {"path": rel, "bytes": len(data), "_artifact": {"path": rel, "bytes": len(data), "digest": digest(data)}}


class WorkspaceReadTool(ToolAdapter):
    name = "workspace.read"
    description = "Read a text artifact from your own workspace (relative path)."
    action_class = "workspace.read"
    args_schema = {"path": "str"}

    def classify(self, args: dict[str, Any]) -> str:
        return ESCAPE_CLASS if isinstance(args, dict) and _escapes(args.get("path")) else self.action_class

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        target = _confined(ctx.workspace, args["path"])
        if not target.is_file():
            raise ToolError("no such artifact")
        return {"path": args["path"], "content": target.read_text("utf-8")[:4000]}
